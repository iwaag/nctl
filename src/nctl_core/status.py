"""Pure library logic for `nctl status` (Step 0.6): the reference implementation of the
envelope, event log, and independent-degradation conventions.

Phase 4 Decision 1: `nctl status` answers one question only -- are Nautobot, local
observations, and repository inputs available/fresh enough to work? It is controller/input
health, not a second per-target drift command; per-target converged/drifting/unknown state is
`nctl drift [--host NODE]`'s job. `nctl.status.v1` and this module's data model do not change
for this -- `render_status_text` just adds one line pointing readers at `nctl drift`.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

from nctl_core.config import Config, ConfigError
from nctl_core.dumps import scan_dumps
from nctl_core.events import OperationLog
from nctl_core.nautobot import NautobotClient, NautobotConnectionError, NautobotInfo
from nctl_core.output import Envelope, EnvelopeError

STATUS_SCHEMA = "nctl.status.v1"


class DumpHostSummary(BaseModel):
    hostname: str
    collected_at: datetime
    age_hours: float


class DumpsStatus(BaseModel):
    dir: str
    hosts: list[DumpHostSummary]
    errors: list[str] = []


class SubmoduleStatus(BaseModel):
    name: str
    commit: str
    state: str  # clean | modified | uninitialized | out-of-sync | conflict


class SubmoduleCheckError(Exception):
    pass


# G1 (no_guest_vm follow-up): a Job stuck PENDING longer than this while a
# worker reports as running is the recorded stall signature -- the worker's
# consumer connection is dead even though `celery inspect ping` still answers.
PENDING_JOB_STALL_SECONDS = 120


class WorkerStatus(BaseModel):
    checked: bool = False
    workers_running: int | None = None
    pending_jobs: int | None = None
    oldest_pending_age_seconds: float | None = None


class StatusData(BaseModel):
    operation_id: str
    nautobot: NautobotInfo
    dumps: DumpsStatus
    submodules: list[SubmoduleStatus]
    worker: WorkerStatus = WorkerStatus()


def build_status(cfg: Config) -> Envelope[StatusData]:
    op = OperationLog.start("status", cfg.events.resolved_log_dir())
    errors: list[EnvelopeError] = []

    op.emit("step_started", "checking nautobot")
    nautobot_info, nautobot_error = _check_nautobot(cfg)
    if nautobot_error is not None:
        errors.append(nautobot_error)
    op.emit("step_completed", "nautobot checked", ok=nautobot_error is None)

    op.emit("step_started", "checking worker")
    worker_status, worker_errors = _check_worker(cfg, nautobot_info)
    errors.extend(worker_errors)
    op.emit("step_completed", "worker checked", ok=not worker_errors)

    op.emit("step_started", "scanning dumps")
    dumps_status = _check_dumps(cfg)
    errors.extend(EnvelopeError(code="dump_parse_error", message=msg) for msg in dumps_status.errors)
    op.emit("step_completed", "dumps scanned", host_count=len(dumps_status.hosts))

    op.emit("step_started", "checking submodules")
    submodules, submodule_error = _check_submodules(cfg)
    if submodule_error is not None:
        errors.append(submodule_error)
    op.emit("step_completed", "submodules checked", ok=submodule_error is None)

    ok = not errors
    op.finish(ok=ok)

    data = StatusData(
        operation_id=op.operation_id,
        nautobot=nautobot_info,
        dumps=dumps_status,
        submodules=submodules,
        worker=worker_status,
    )
    return Envelope.build(STATUS_SCHEMA, data, errors)


def render_status_text(envelope: Envelope[StatusData]) -> str:
    data = envelope.data
    lines = []

    nb = data.nautobot
    mark = "✓" if nb.reachable and nb.authenticated else "✗"
    lines.append(f"{mark} nautobot   {nb.url}")
    if nb.reachable:
        lines.append(
            f"    version: {nb.version}, authenticated: {nb.authenticated}, "
            f"intent_catalog: {nb.intent_catalog}, intent_graphql: {nb.intent_graphql}"
        )
    else:
        lines.append("    unreachable")

    if data.worker.checked:
        stalled = any(err.code in ("celery_workers_not_running", "worker_queue_stalled") for err in envelope.errors)
        wmark = "✗" if stalled else "✓"
        pending = data.worker.pending_jobs if data.worker.pending_jobs is not None else "?"
        lines.append(f"{wmark} worker     celery workers: {data.worker.workers_running}, pending jobs: {pending}")
        if data.worker.oldest_pending_age_seconds is not None:
            lines.append(f"    oldest pending job: {data.worker.oldest_pending_age_seconds:.0f}s")

    dmark = "✓" if not data.dumps.errors else "✗"
    lines.append(f"{dmark} dumps      {data.dumps.dir} ({len(data.dumps.hosts)} host(s))")
    for host in data.dumps.hosts:
        lines.append(f"    {host.hostname}: collected {host.age_hours:.1f}h ago")
    for err in data.dumps.errors:
        lines.append(f"    ! {err}")

    for sub in data.submodules:
        smark = "✓" if sub.state == "clean" else "✗"
        lines.append(f"{smark} submodule  {sub.name} @ {sub.commit[:12]} ({sub.state})")

    for err in envelope.errors:
        lines.append(f"error [{err.code}]: {err.message}")

    lines.append(f"ok: {envelope.ok}")
    lines.append("target state: use `nctl drift --host SLUG`")
    return "\n".join(lines)


def _check_nautobot(cfg: Config) -> tuple[NautobotInfo, EnvelopeError | None]:
    try:
        token = cfg.nautobot.resolve_token()
    except ConfigError as exc:
        return NautobotInfo(reachable=False, url=cfg.nautobot.url), EnvelopeError(
            code="nautobot_token_error", message=str(exc)
        )

    client = NautobotClient(cfg.nautobot.url, token)
    try:
        info = client.ping()
    except NautobotConnectionError as exc:
        return NautobotInfo(reachable=False, url=cfg.nautobot.url), EnvelopeError(
            code="nautobot_unreachable", message=str(exc)
        )
    finally:
        client.close()

    if not info.authenticated:
        return info, EnvelopeError(
            code="nautobot_unauthenticated", message=f"authentication failed against {cfg.nautobot.url}"
        )
    if info.intent_catalog and not info.intent_graphql:
        return info, EnvelopeError(
            code="intent_graphql_missing",
            message=f"intent-catalog GraphQL types not found in the schema at {cfg.nautobot.url}",
        )
    return info, None


def _check_worker(cfg: Config, nautobot_info: NautobotInfo) -> tuple[WorkerStatus, list[EnvelopeError]]:
    """G1 detector: is the Celery worker present *and actually consuming*?

    Two independent signals, both read-only over the Nautobot REST API:
    worker registration (`/api/status/` `celery-workers-running`) catches a
    dead worker; a PENDING JobResult older than `PENDING_JOB_STALL_SECONDS`
    catches the silent-stall mode where the worker still answers pings but
    its consumer connection is dead (recorded 2026-08-06 and 2026-08-10).
    """

    status = WorkerStatus()
    if not (nautobot_info.reachable and nautobot_info.authenticated):
        return status, []
    errors: list[EnvelopeError] = []
    try:
        token = cfg.nautobot.resolve_token()
        with NautobotClient(cfg.nautobot.url, token) as client:
            response = client.rest_get("/api/status/")
            response.raise_for_status()
            workers = response.json().get("celery-workers-running")
            status.workers_running = workers if isinstance(workers, int) else None
            response = client.rest_get("/api/extras/job-results/", params={"status": "PENDING", "limit": 50})
            response.raise_for_status()
            payload = response.json()
            status.pending_jobs = payload.get("count")
            now = datetime.now(timezone.utc)
            ages = []
            for row in payload.get("results", []):
                created = row.get("date_created")
                if not created:
                    continue
                try:
                    ages.append((now - datetime.fromisoformat(created)).total_seconds())
                except ValueError:
                    continue
            status.oldest_pending_age_seconds = max(ages) if ages else None
    except Exception as exc:  # noqa: BLE001 - a degraded probe must not sink the other checks
        return status, [EnvelopeError(code="worker_check_failed", message=f"worker probe failed: {exc}")]
    status.checked = True
    if status.workers_running == 0:
        errors.append(
            EnvelopeError(
                code="celery_workers_not_running",
                message="Nautobot reports no running Celery worker; Job submissions will fail with HTTP 503",
            )
        )
    elif status.oldest_pending_age_seconds is not None and status.oldest_pending_age_seconds > PENDING_JOB_STALL_SECONDS:
        errors.append(
            EnvelopeError(
                code="worker_queue_stalled",
                message=(
                    f"{status.pending_jobs} Job(s) stuck PENDING (oldest {status.oldest_pending_age_seconds:.0f}s) "
                    "while a worker is registered -- the worker's consumer connection is likely dead; "
                    "restart the worker container (scratch env: `docker restart nautobot-nautobot-worker-1`)"
                ),
            )
        )
    return status, errors


def _check_dumps(cfg: Config) -> DumpsStatus:
    dumps_dir = cfg.inventory.resolved_dumps_dir()
    result = scan_dumps(dumps_dir)
    now = datetime.now(timezone.utc)
    hosts = [
        DumpHostSummary(
            hostname=dump.identity.hostname,
            collected_at=dump.collected_at,
            age_hours=(now - dump.collected_at).total_seconds() / 3600,
        )
        for dump in result.dumps
    ]
    return DumpsStatus(dir=str(dumps_dir), hosts=hosts, errors=result.errors)


def _check_submodules(cfg: Config) -> tuple[list[SubmoduleStatus], EnvelopeError | None]:
    try:
        return _git_submodule_status(cfg.repo_root()), None
    except SubmoduleCheckError as exc:
        return [], EnvelopeError(code="submodule_check_failed", message=str(exc))


def _git_submodule_status(repo_root: Path) -> list[SubmoduleStatus]:
    try:
        result = subprocess.run(
            ["git", "submodule", "status"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SubmoduleCheckError(f"git submodule status failed: {exc}") from exc
    if result.returncode != 0:
        raise SubmoduleCheckError(f"git submodule status failed: {result.stderr.strip()}")

    prefix_states = {"-": "uninitialized", "+": "out-of-sync", "U": "conflict"}
    submodules = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        prefix, rest = line[0], line[1:]
        parts = rest.split(" ", 2)
        commit = parts[0]
        path = parts[1] if len(parts) > 1 else ""
        state = prefix_states.get(prefix, "clean")
        if state == "clean" and _is_dirty(repo_root / path):
            state = "modified"
        submodules.append(SubmoduleStatus(name=Path(path).name, commit=commit, state=state))
    return submodules


def _is_dirty(submodule_path: Path) -> bool:
    if not submodule_path.is_dir():
        return False
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=submodule_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.returncode == 0 and bool(result.stdout.strip())
