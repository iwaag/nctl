"""`nctl observe`: observation-only collection and ingest (project_migration p5).

Runs exactly the `observe_node` pipeline (`observation.run_observation`:
nodeutils collection over SSH, report retrieval, validation, cache, the
Nautobot ingest Job) for an explicit host set, and nothing else. Drift is
never computed and no reconciler is planned, so drift a host happens to show
is not a reason to change it: observing and converging are separate
operations (`nctl reconcile` is the other one).

Host resolution never widens the target set: an explicit host that does not
resolve to exactly one desired node is a usage error before anything runs;
with no host, the scope is every node the bootstrap inventory exports
(active lifecycle with an mDNS endpoint), and the nodes it skips are listed
with their reasons. A host whose managed SSH key is missing, or whose route
offers no trusted key (an asleep laptop, a mismatch), is `unavailable` and is
not contacted; the others are still observed.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field

from nctl_core.artifacts import ArtifactError, OperationArtifacts
from nctl_core.config import Config, ConfigError
from nctl_core.events import OperationLog
from nctl_core.hosts_intent import export_hosts_intent
from nctl_core.nautobot import NautobotClient, NautobotError
from nctl_core.observation import run_observation
from nctl_core.operations_index import OPERATION_ID_RE
from nctl_core.output import Envelope, EnvelopeError
from nctl_core.reconcile.lock import ReconcileLockError, acquire_reconcile_lock
from nctl_core.reconcile.ssh_preflight import STATUS_READY, verify_offered_keys
from nctl_core.sources.desired import DesiredSnapshot, fetch_desired_snapshot
from nctl_core.ssh_enroll import SshProbeRunner, SshStoreReadError, default_ssh_probe_runner

OBSERVE_SCHEMA = "nctl.observe.v1"

# Per-host outcomes. Only `observed` means fresh evidence reached Nautobot.
OBSERVED = "observed"
UNAVAILABLE = "unavailable"          # not contacted: no trusted key on a reachable route
COLLECTION_FAILED = "collection_failed"
INGEST_FAILED = "ingest_failed"


class UsageError(Exception):
    """The request itself is wrong (unknown host, malformed operation id): nothing ran."""


class SourceUnavailable(Exception):
    """Desired state could not be read, so no host set could be resolved: nothing ran."""


class HostOutcome(BaseModel):
    host: str
    outcome: str
    detail: str | None = None
    ingest_outcome: str | None = None
    ssh_status: str | None = None


class ObserveData(BaseModel):
    operation_id: str
    state: str = "failed"   # observed | partial | failed
    scope: str = "explicit"  # explicit | default
    requested: list[str] = Field(default_factory=list)
    skipped: list[dict[str, Any]] = Field(default_factory=list)  # default scope: nodes not exported, with reasons
    hosts: list[HostOutcome] = Field(default_factory=list)
    nodeutils_version: str = ""
    ingest_job: dict[str, Any] | None = None
    event_log_path: str = ""
    artifact_dir: str = ""


def check_operation_id(operation_id: str | None, log_dir: Path) -> str | None:
    """A caller-chosen id must be a ULID not used before (the caller records it before the effect)."""
    if operation_id is None:
        return None
    if not OPERATION_ID_RE.match(operation_id):
        raise UsageError(f"malformed operation id: {operation_id!r} (a 26-character ULID)")
    if (log_dir / f"{operation_id}.jsonl").exists() or (log_dir / operation_id).exists():
        raise UsageError(f"operation id {operation_id} is already in use")
    return operation_id


def resolve_hosts(snapshot: DesiredSnapshot, requested: list[str], cfg: Config) -> tuple[list[str], list[dict[str, Any]]]:
    export = export_hosts_intent(
        snapshot.nodes, snapshot.endpoints, ssh_known_hosts_file=str(cfg.resolved_ssh_known_hosts_file())
    )
    eligible = sorted(row["inventory_hostname"] for row in export.hosts)
    if not requested:
        skipped = [entry for entry in export.skipped if entry.get("item_type") == "desired_node"]
        return eligible, skipped
    problems = []
    for host in requested:
        matches = [node for node in snapshot.nodes if node.slug == host]
        if len(matches) != 1:
            problems.append(f"{host!r} matches {len(matches)} desired nodes")
        elif host not in eligible:
            problems.append(f"{host!r} is not observable (not exported by the bootstrap inventory: inactive or no mDNS endpoint)")
    if problems:
        raise UsageError("; ".join(problems))
    return sorted(set(requested)), []


def run_observe(
    cfg: Config,
    hosts: list[str],
    *,
    operation_id: str | None = None,
    command_runner: Any | None = None,
    job_runner: Any | None = None,
    snapshot_fetcher: Callable[[], DesiredSnapshot] | None = None,
    ssh_probe: SshProbeRunner | None = None,
    now: Callable[[], datetime] | None = None,
) -> Envelope[ObserveData]:
    """Raises `UsageError` before any operation record when the request is wrong."""
    now = now or (lambda: datetime.now(timezone.utc))
    log_dir = cfg.events.resolved_log_dir()
    check_operation_id(operation_id, log_dir)
    try:
        snapshot = snapshot_fetcher() if snapshot_fetcher else _fetch_desired(cfg)
    except (ConfigError, NautobotError) as exc:
        raise SourceUnavailable(str(exc)) from exc
    targets, skipped = resolve_hosts(snapshot, list(hosts), cfg)

    op = OperationLog("observe", log_dir, operation_id=operation_id)
    op.emit("started", "observe started", hosts=targets)
    data = ObserveData(
        operation_id=op.operation_id, scope="explicit" if hosts else "default", requested=targets,
        skipped=skipped, event_log_path=str(op.path),
    )
    try:
        artifacts = OperationArtifacts.create(log_dir, op.operation_id)
    except ArtifactError as exc:
        return _finish(op, data, [EnvelopeError(code="artifact_write_failed", message=str(exc))])
    data.artifact_dir = str(artifacts.root)

    try:
        with acquire_reconcile_lock(cfg.reconcile.resolved_lock_path()):
            return _observe_locked(cfg, op, data, artifacts, snapshot, targets, command_runner, job_runner,
                                   ssh_probe or default_ssh_probe_runner(), now)
    except ReconcileLockError as exc:
        return _finish(op, data, [EnvelopeError(code="reconcile_lock_contention", message=str(exc))])


def _fetch_desired(cfg: Config) -> DesiredSnapshot:
    client = NautobotClient(cfg.nautobot.url, cfg.nautobot.resolve_token())
    try:
        return fetch_desired_snapshot(client)
    finally:
        client.close()


def _observe_locked(cfg, op, data, artifacts, snapshot, targets, command_runner, job_runner, ssh_probe, now):
    try:
        preflight = verify_offered_keys(cfg, targets, snapshot, ssh_probe)
    except SshStoreReadError as exc:
        return _finish(op, data, [EnvelopeError(code="ssh_store_read_failed", message=str(exc))])
    artifacts.write_json("ssh-preflight.json", [entry.model_dump() for entry in preflight])
    by_host = {entry.slug: entry for entry in preflight}
    ready = [host for host in targets if by_host[host].status == STATUS_READY]
    outcomes: dict[str, HostOutcome] = {}
    for host in targets:
        entry = by_host[host]
        if entry.status != STATUS_READY:
            outcomes[host] = HostOutcome(host=host, outcome=UNAVAILABLE, ssh_status=entry.status,
                                         detail=entry.detail or f"ssh {entry.status}; not contacted")
    op.emit("preflight_completed", "ssh preflight completed", ready=ready,
            unavailable=sorted(h for h in targets if h not in ready))

    if ready:
        try:
            result = run_observation(cfg, snapshot, ready, artifacts, op, command_runner=command_runner,
                                     job_runner=job_runner, now=now())
        except (ValueError, SshStoreReadError) as exc:
            for host in ready:
                outcomes[host] = HostOutcome(host=host, outcome=COLLECTION_FAILED, ssh_status=STATUS_READY, detail=str(exc))
        else:
            data.nodeutils_version = result.nodeutils_version
            if result.job is not None:
                data.ingest_job = {"status": result.job.status, "job_result_id": result.job.job_result_id}
            for row in result.hosts:
                if row.collected and row.ingest_outcome in {"created", "updated", "unchanged"} and not row.error:
                    outcome = OBSERVED
                elif row.collected:
                    outcome = INGEST_FAILED
                else:
                    outcome = COLLECTION_FAILED
                outcomes[row.host] = HostOutcome(host=row.host, outcome=outcome, detail=row.error,
                                                 ingest_outcome=row.ingest_outcome, ssh_status=STATUS_READY)
    data.hosts = [outcomes[host] for host in targets]
    errors = []
    for code in (UNAVAILABLE, COLLECTION_FAILED, INGEST_FAILED):
        failed = [h.host for h in data.hosts if h.outcome == code]
        if failed:
            errors.append(EnvelopeError(code=f"host_{code}", message=f"{code}: {', '.join(failed)}", detail={"hosts": failed}))
    return _finish(op, data, errors)


def _finish(op: OperationLog, data: ObserveData, errors: list[EnvelopeError]) -> Envelope[ObserveData]:
    observed = [h for h in data.hosts if h.outcome == OBSERVED]
    if data.hosts and len(observed) == len(data.hosts) and not errors:
        data.state = "observed"
    elif observed:
        data.state = "partial"
    else:
        data.state = "failed"
    ok = data.state == "observed"
    envelope = Envelope(schema=OBSERVE_SCHEMA, generated_at=datetime.now(timezone.utc), ok=ok, data=data, errors=errors)
    if data.artifact_dir:
        try:
            path = OperationArtifacts(Path(data.artifact_dir)).write_json(
                "result.json", envelope.model_dump(mode="json", by_alias=True))
            os.chmod(path, 0o644)
        except (ArtifactError, OSError):
            pass
    op.finish(ok=ok, message=data.state)
    return envelope


def render_observe_text(envelope: Envelope[ObserveData]) -> str:
    data = envelope.data
    lines = [f"observe {data.operation_id}: {data.state} ({data.scope} scope)"]
    for host in data.hosts:
        lines.append(f"  {host.host:24} {host.outcome}" + (f" — {host.detail}" if host.detail and host.outcome != OBSERVED else ""))
    for entry in data.skipped:
        lines.append(f"  (not in scope) {entry.get('desired_node_slug')}: {', '.join(entry.get('reasons', []))}")
    for error in envelope.errors:
        lines.append(f"error {error.code}: {error.message}")
    return "\n".join(lines)
