"""`nctl drift`: fetch + compute as one synchronous call (Phase 2 Step 5).

Drift is a read like `render dnsmasq`/`render production` (no operation ID,
no event log — those stay reserved for Phase 4's long-running `apply`/
`reconcile`), but it *reads* the operations directory itself
(`context.events_dir`, via `drift.status.derive_status`'s `converging` rule)
without writing anything to it.

Unlike a render failing to produce a document, drift finding disagreements is
the expected, successful case ("AI can read just [drift] to explain the
current state" only holds if drift itself never errors out over a disagreeing
node) — `envelope.ok` and the exit code only go false when the run *itself*
fails (bad token, unreachable Nautobot, unreadable dump directory propagating
as `NautobotError`), never because a target came back `drifting`/`unknown`.
A missing or invalid `vars/deployment_profiles.yml` is treated the same way
`production_policy` already treats an absent profiles map internally
(`comparators.py`: "if not context.profiles: return") — degraded to `{}`
rather than failing the whole run, since a drift command that goes dark
because of one unrelated file is worse than a drift command that just runs
every comparator except `production_policy`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel

from nctl_core.config import Config, ConfigError
from nctl_core.drift.context import DriftContext
from nctl_core.drift.engine import TargetStatus, compute_drift
from nctl_core.drift.model import Severity
from nctl_core.nautobot import NautobotClient, NautobotError
from nctl_core.output import Envelope, EnvelopeError
from nctl_core.production.profiles import DeploymentProfilesError, load_deployment_profiles
from nctl_core.sources.snapshot import build_source_snapshot

DRIFT_SCHEMA = "nctl.drift.v1"


class DriftSourcesData(BaseModel):
    fetched_at: str = ""
    observed_dump_count: int = 0
    observed_errors: list[str] = []


class DriftData(BaseModel):
    generated_at: str = ""
    summary: dict[str, int] = {}
    severity_summary: dict[str, int] = {}
    targets: list[TargetStatus] = []
    sources: DriftSourcesData = DriftSourcesData()


def build_drift(cfg: Config, *, host: str | None = None, service: str | None = None) -> Envelope[DriftData]:
    generated_at = datetime.now(timezone.utc).isoformat()

    try:
        token = cfg.nautobot.resolve_token()
    except ConfigError as exc:
        return _failed(EnvelopeError(code="nautobot_token_error", message=str(exc)))

    playbook_dir = cfg.ansible.resolved_playbook_dir(cfg.source_path.parent)
    try:
        profiles, _digest = load_deployment_profiles(playbook_dir)
    except DeploymentProfilesError:
        profiles = {}

    client = NautobotClient(cfg.nautobot.url, token)
    try:
        snapshot = build_source_snapshot(cfg, client)
    except NautobotError as exc:
        return _failed(EnvelopeError(code="nautobot_fetch_failed", message=str(exc)))
    finally:
        client.close()

    context = DriftContext(generated_at=generated_at, profiles=profiles, events_dir=cfg.events.resolved_log_dir())
    result = compute_drift(snapshot, context)
    targets = _filter_targets(result.targets, host=host, service=service)

    data = DriftData(
        generated_at=generated_at,
        summary=_status_summary(targets),
        severity_summary=_severity_summary(targets),
        targets=targets,
        sources=DriftSourcesData(
            fetched_at=snapshot.fetched_at.isoformat(),
            observed_dump_count=len(snapshot.observed),
            observed_errors=snapshot.observed_errors,
        ),
    )
    return Envelope.build(DRIFT_SCHEMA, data, [])


def _filter_targets(targets: list[TargetStatus], *, host: str | None, service: str | None) -> list[TargetStatus]:
    if host is not None:
        targets = [t for t in targets if t.target.kind == "node" and t.target.slug == host]
    if service is not None:
        targets = [t for t in targets if t.target.kind == "service" and t.target.name == service]
    return targets


def _status_summary(targets: list[TargetStatus]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for target_status in targets:
        summary[target_status.status.value] = summary.get(target_status.status.value, 0) + 1
    return summary


def _severity_summary(targets: list[TargetStatus]) -> dict[str, int]:
    summary = {severity.value: 0 for severity in Severity}
    for target_status in targets:
        for diff in target_status.diffs:
            summary[diff.severity.value] += 1
    return summary


def render_drift_text(envelope: Envelope[DriftData]) -> str:
    if not envelope.ok:
        return "\n".join(f"error [{err.code}]: {err.message}" for err in envelope.errors)

    data = envelope.data
    lines: list[str] = []
    for target_status in data.targets:
        label = target_status.target.slug or target_status.target.name or target_status.target.id or "?"
        lines.append(f"{label}  {target_status.status.value}  {len(target_status.diffs)} diff(s)")
        for diff in target_status.diffs:
            lines.append(f"    [{diff.severity.value}] {diff.message}")

    status_line = " ".join(f"{status}={count}" for status, count in sorted(data.summary.items()))
    lines.append(f"summary: {status_line}" if status_line else "summary: (no targets)")
    return "\n".join(lines)


def _failed(error: EnvelopeError) -> Envelope[DriftData]:
    return Envelope.build(DRIFT_SCHEMA, DriftData(), [error])
