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

A missing or invalid `vars/deployment_profiles.yml` does not fail the drift
command either (`envelope.ok` stays `True`; a drift command going dark
because of one unrelated file is worse than a drift command that just skips
`production_policy`'s composition). Phase 4 Decision 3 changed what it
*does* surface: `DeploymentProfilesError` is threaded through as
`DriftContext.profiles_error` rather than silently degrading to `{}`, and
`production_policy` turns that into a classified global ERROR
`deployment_profiles_unavailable` target -- visible, and blocking for
`nctl reconcile` (global `MANUAL_REVIEW`), without blocking every other
comparator or the drift run itself.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel

from nctl_core.config import Config, ConfigError
from nctl_core.drift.context import DriftContext
from nctl_core.drift.engine import DriftResult, TargetStatus, compute_drift
from nctl_core.drift.model import DiffRecord, Severity
from nctl_core.nautobot import NautobotClient, NautobotError
from nctl_core.output import Envelope, EnvelopeError
from nctl_core.production.profiles import DeploymentProfilesError, load_deployment_profiles
from nctl_core.reconcile.profiles import ProfileReconciliationError, load_profile_reconciliation
from nctl_core.sources.snapshot import SourceSnapshot, build_source_snapshot

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
    fetched = fetch_and_compute_drift(cfg)
    if isinstance(fetched, EnvelopeError):
        return _failed(fetched)
    snapshot, result, generated_at = fetched
    data = render_drift_data(result, generated_at, snapshot, host=host, service=service)
    return Envelope.build(DRIFT_SCHEMA, data, [])


def render_drift_data(
    result: DriftResult,
    generated_at: str,
    snapshot: SourceSnapshot,
    *,
    host: str | None = None,
    service: str | None = None,
) -> DriftData:
    """Render a `DriftResult` (already computed by `fetch_and_compute_drift`) as `DriftData`.

    Shared with `nctl reconcile` (Phase 4 Step 7), which computes its own
    full-cluster drift each round and must render it identically to `nctl
    drift`/`nctl dashboard` rather than reimplementing this shape.
    """

    targets = _filter_targets(result.targets, host=host, service=service)
    return DriftData(
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


def fetch_and_compute_drift(
    cfg: Config,
) -> tuple[SourceSnapshot, DriftResult, str] | EnvelopeError:
    """Fetch the full-cluster snapshot and compute drift over it, unfiltered.

    Shared by `build_drift` (which then filters/renders `nctl.drift.v1`) and
    `nctl reconcile` (Phase 4 Step 7), which needs the raw `SourceSnapshot`
    itself to build a plan, not just the rendered per-target diff list.
    Returns `(snapshot, DriftResult, generated_at)` on success.
    """

    generated_at = datetime.now(timezone.utc).isoformat()

    try:
        token = cfg.nautobot.resolve_token()
    except ConfigError as exc:
        return EnvelopeError(code="nautobot_token_error", message=str(exc))

    playbook_dir = cfg.ansible.resolved_playbook_dir(cfg.source_path.parent)
    profiles_error: str | None = None
    profile_reconciliation: dict = {}
    profile_reconciliation_error: str | None = None
    try:
        profiles, _digest = load_deployment_profiles(playbook_dir)
    except DeploymentProfilesError as exc:
        profiles = {}
        profiles_error = str(exc)
        # Reconciliation metadata is keyed against the validated profile name
        # set; with no valid profiles there is nothing to validate it against.
        profile_reconciliation_error = str(exc)
    else:
        try:
            profile_reconciliation = load_profile_reconciliation(playbook_dir, set(profiles))
        except ProfileReconciliationError as exc:
            profile_reconciliation_error = str(exc)

    client = NautobotClient(cfg.nautobot.url, token)
    try:
        snapshot = build_source_snapshot(cfg, client)
    except NautobotError as exc:
        return EnvelopeError(code="nautobot_fetch_failed", message=str(exc))
    finally:
        client.close()

    context = DriftContext(
        generated_at=generated_at,
        profiles=profiles,
        profiles_error=profiles_error,
        profile_reconciliation=profile_reconciliation,
        profile_reconciliation_error=profile_reconciliation_error,
        events_dir=cfg.events.resolved_log_dir(),
        service_observation_max_age_hours=cfg.reconcile.service_observation_max_age_hours,
    )
    result = compute_drift(snapshot, context)
    return snapshot, result, generated_at


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
            if diff.code == "intent_effect_summary":
                lines.extend(_intent_effect_summary_lines(diff))
            else:
                lines.append(f"    [{diff.severity.value}] {diff.message}")

    status_line = " ".join(f"{status}={count}" for status, count in sorted(data.summary.items()))
    lines.append(f"summary: {status_line}" if status_line else "summary: (no targets)")
    return "\n".join(lines)


def _intent_effect_summary_lines(diff: DiffRecord) -> list[str]:
    """Render `intent_effect_summary` as three compact, deterministic lines
    (Phase 4 Decision 2/Step 4.5 item 3) instead of the generic message line:
    recorded intent, effective derived/default/override mechanism, and
    production/placement application. Prints placement config *keys* only via
    the caller never being handed config values here at all -- full config
    remains in JSON evidence (`--json`), never dumped into this text.
    """

    node = diff.desired["node"]
    intent_parts = [
        f"lifecycle={node['lifecycle']}",
        f"node_type={node['node_type']}",
        f"accepted_actual_types={','.join(node['accepted_actual_types']) or '(none)'} "
        f"({node['accepted_actual_types_source']})",
    ]
    placements = diff.desired["placements"]
    if placements:
        intent_parts.append(
            "placements: "
            + ", ".join(
                f"{p['instance_name']}({p['service_slug']}/{p['desired_state']}/profile={p['deployment_profile']}"
                f"/config_keys={sorted(p['config'])})"
                for p in placements
            )
        )
    intent_line = "    [info] intent: " + " ".join(intent_parts)

    operational_values = diff.actual["operational_values"]
    finding = diff.actual["operational_finding"]
    if finding is not None:
        effective_line = f"    [info] effective: derivation failed ({finding['code']}: {finding['message']})"
    elif operational_values:
        rendered_values = " ".join(
            f"{field}={record['value']} ({record['source']})"
            for field, record in sorted(operational_values.items())
        )
        effective_line = f"    [info] effective: {rendered_values}"
    else:
        effective_line = "    [info] effective: (not computed)"

    production = diff.actual["production"]
    application_parts = [f"state={production['state']}"]
    if production["reasons"]:
        application_parts.append(f"reasons={','.join(production['reasons'])}")
    for effect in production["placement_effects"]:
        suffix = f" ({effect['reason']})" if effect["reason"] else ""
        application_parts.append(f"{effect['instance_name']}={effect['effect']}{suffix}")
    application_line = "    [info] application: " + " ".join(application_parts)

    return [intent_line, effective_line, application_line]


def _failed(error: EnvelopeError) -> Envelope[DriftData]:
    return Envelope.build(DRIFT_SCHEMA, DriftData(), [error])
