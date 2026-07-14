"""Pure library logic for `nctl status` (Step 0.6): the reference implementation of the
envelope, event log, and independent-degradation conventions.
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


class StatusData(BaseModel):
    operation_id: str
    nautobot: NautobotInfo
    dumps: DumpsStatus
    submodules: list[SubmoduleStatus]


def build_status(cfg: Config) -> Envelope[StatusData]:
    op = OperationLog.start("status", cfg.events.resolved_log_dir())
    errors: list[EnvelopeError] = []

    op.emit("step_started", "checking nautobot")
    nautobot_info, nautobot_error = _check_nautobot(cfg)
    if nautobot_error is not None:
        errors.append(nautobot_error)
    op.emit("step_completed", "nautobot checked", ok=nautobot_error is None)

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
    )
    return Envelope.build(STATUS_SCHEMA, data, errors)


def render_status_text(envelope: Envelope[StatusData]) -> str:
    data = envelope.data
    lines = []

    nb = data.nautobot
    mark = "✓" if nb.reachable and nb.authenticated else "✗"
    lines.append(f"{mark} nautobot   {nb.url}")
    if nb.reachable:
        lines.append(f"    version: {nb.version}, authenticated: {nb.authenticated}, intent_catalog: {nb.intent_catalog}")
    else:
        lines.append("    unreachable")

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
    return info, None


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
