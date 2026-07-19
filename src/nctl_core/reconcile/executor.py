"""`nctl reconcile`: the bounded plan/apply executor (Phase 4 Step 7).

Ties Step 5's planner and Step 6's ledger execution into one operation:
drift -> plan -> (plan mode stops here) -> execute actions in DAG order ->
fresh observation -> final drift -> dashboard. See `p4/plan.md`'s "Apply
mode execution per round" for the numbered steps this module implements.

The round loop is deliberately collapsed relative to the plan's 9 numbered
steps: each iteration begins by fetching one fresh full-cluster drift and
building a plan for the requested scope (plan.md's steps 1-3). If the plan
has nothing left to do, that same drift *is* the final drift (nothing was
mutated this round, so there is nothing to re-observe). Otherwise actions
execute (steps 4-8, split into a bootstrap/ledger phase, a production
inventory regeneration, and a service/dnsmasq phase) and the loop
continues -- the *next* iteration's fresh fetch is round N's "after" drift,
avoiding a second, potentially disagreeing, drift computation for the same
round.
"""

from __future__ import annotations

import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field

from nctl_core.ansible import AnsibleRunner, CommandRunner
from nctl_core.artifacts import ArtifactError, OperationArtifacts
from nctl_core.config import Config, ConfigError
from nctl_core.dashboard_render import DashboardData, render_dashboard_from_drift
from nctl_core.dnsmasq_apply import build_dnsmasq_apply
from nctl_core.drift.engine import DriftResult, TargetStatus
from nctl_core.drift_render import DriftData, fetch_and_compute_drift, render_drift_data
from nctl_core.events import OperationLog
from nctl_core.jobs import NautobotJobError, NautobotJobRunner
from nctl_core.nautobot import NautobotClient, NautobotError
from nctl_core.observation import ObservationResult, run_observation
from nctl_core.output import Envelope, EnvelopeError
from nctl_core.production.profiles import DeploymentProfilesError, load_deployment_profiles
from nctl_core.production_render import build_production_render, write_production_artifacts
from nctl_core.sources.snapshot import SourceSnapshot

from .classify import UnclassifiedDiffCodeError
from .ledger import LedgerActionError, execute_link_actual_node, execute_reconcile_ipam
from .lock import ReconcileLockError, acquire_reconcile_lock
from .model import PlanScope, ReconcileAction, ReconcilePlan
from .planner import HostScopeError, build_plan
from .profiles import ProfileReconciliationError, load_profile_reconciliation

RECONCILE_SCHEMA = "nctl.reconcile.v1"

_BOOTSTRAP_LEDGER_RECONCILERS = frozenset({"observe_node", "link_actual_node", "reconcile_ipam"})


class ActionResult(BaseModel):
    action_id: str
    reconciler_id: str
    action_kind: str
    target_slugs: list[str] = Field(default_factory=list)
    success: bool
    detail: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class RoundSummary(BaseModel):
    round: int
    drift_fingerprint: str
    actions: list[ActionResult] = Field(default_factory=list)


class ReconcileData(BaseModel):
    operation_id: str
    mode: str
    scope: PlanScope
    state: str = "failed"
    event_log_path: str
    artifact_dir: str = ""
    plan_path: str = ""
    initial_drift_path: str = ""
    final_drift_path: str = ""
    rounds: list[RoundSummary] = Field(default_factory=list)
    manual_review: list[dict[str, Any]] = Field(default_factory=list)
    unsupported: list[dict[str, Any]] = Field(default_factory=list)
    summary: dict[str, int] = Field(default_factory=dict)
    scope_summary: dict[str, int] = Field(default_factory=dict)
    dashboard: DashboardData | None = None
    progress_made: bool = False


class _Interrupted(Exception):
    pass


def run_reconcile(
    cfg: Config,
    *,
    host: str | None = None,
    apply_changes: bool = False,
    max_rounds: int | None = None,
    now: Callable[[], datetime] | None = None,
    command_runner: CommandRunner | None = None,
    operation_id: str | None = None,
) -> Envelope[ReconcileData]:
    now = now or (lambda: datetime.now(timezone.utc))
    scope = PlanScope(kind="host", host_slug=host) if host else PlanScope(kind="cluster")
    op = OperationLog("reconcile", cfg.events.resolved_log_dir(), operation_id=operation_id)
    op.emit("started", "reconcile started")
    data = ReconcileData(
        operation_id=op.operation_id,
        mode="apply" if apply_changes else "plan",
        scope=scope,
        event_log_path=str(op.path),
    )

    try:
        artifacts = OperationArtifacts.create(cfg.events.resolved_log_dir(), op.operation_id)
    except ArtifactError as exc:
        return _finish(op, data, "failed", [EnvelopeError(code="artifact_write_failed", message=str(exc))])
    data.artifact_dir = str(artifacts.root)

    if not apply_changes:
        return _run_plan_only(cfg, op, data, artifacts, scope)

    try:
        with acquire_reconcile_lock(cfg.reconcile.resolved_lock_path()):
            with _InterruptFlag() as interrupted:
                return _run_apply(cfg, op, data, artifacts, scope, max_rounds, now, command_runner, interrupted)
    except ReconcileLockError as exc:
        return _finish(op, data, "failed", [EnvelopeError(code="reconcile_lock_contention", message=str(exc))])


def _run_plan_only(
    cfg: Config, op: OperationLog, data: ReconcileData, artifacts: OperationArtifacts, scope: PlanScope
) -> Envelope[ReconcileData]:
    fetched = fetch_and_compute_drift(cfg)
    if isinstance(fetched, EnvelopeError):
        return _finish(op, data, "failed", [fetched])
    snapshot, drift_result, generated_at = fetched
    drift_data = render_drift_data(drift_result, generated_at, snapshot)
    data.initial_drift_path = str(artifacts.write_json("round-00/drift-before.json", drift_data.model_dump(mode="json")))

    plan, plan_error = _build_plan_or_error(cfg, snapshot, drift_result, scope, generated_at)
    if plan_error is not None:
        return _finish(op, data, "failed", [plan_error])

    data.plan_path = str(artifacts.write_json("plan.json", plan.model_dump(mode="json")))
    op.emit(
        "plan_created",
        "reconcile plan created",
        drift_fingerprint=plan.drift_fingerprint,
        action_count=len(plan.actions),
    )
    data.manual_review = [record.model_dump(mode="json") for record in plan.manual_review]
    data.unsupported = [record.model_dump(mode="json") for record in plan.unsupported]
    data.summary = drift_data.summary
    data.scope_summary = _scope_summary(drift_result.targets, scope, snapshot)
    return _finish(op, data, "planned", [])


def _run_apply(
    cfg: Config,
    op: OperationLog,
    data: ReconcileData,
    artifacts: OperationArtifacts,
    scope: PlanScope,
    max_rounds: int | None,
    now: Callable[[], datetime],
    command_runner: CommandRunner | None,
    interrupted: "_InterruptFlag",
) -> Envelope[ReconcileData]:
    rounds_limit = max_rounds or cfg.reconcile.max_rounds
    previous_fingerprint: str | None = None
    final_drift_result: DriftResult | None = None
    final_generated_at = ""
    final_snapshot: SourceSnapshot | None = None
    state = "failed"
    errors: list[EnvelopeError] = []

    for round_index in range(rounds_limit):
        if interrupted.is_set():
            state = "failed"
            errors = [EnvelopeError(code="interrupted", message="reconcile was interrupted before this round started")]
            break

        fetched = fetch_and_compute_drift(cfg)
        if isinstance(fetched, EnvelopeError):
            state, errors = "failed", [fetched]
            break
        snapshot, drift_result, generated_at = fetched
        final_drift_result, final_generated_at, final_snapshot = drift_result, generated_at, snapshot
        if round_index == 0:
            drift_data = render_drift_data(drift_result, generated_at, snapshot)
            data.initial_drift_path = str(
                artifacts.write_json("round-00/drift-before.json", drift_data.model_dump(mode="json"))
            )

        plan, plan_error = _build_plan_or_error(cfg, snapshot, drift_result, scope, generated_at)
        if plan_error is not None:
            state, errors = "failed", [plan_error]
            break
        data.plan_path = str(artifacts.write_json("plan.json", plan.model_dump(mode="json")))
        op.emit(
            "plan_created",
            "reconcile plan created",
            drift_fingerprint=plan.drift_fingerprint,
            action_count=len(plan.actions),
            round=round_index,
        )
        data.manual_review = [record.model_dump(mode="json") for record in plan.manual_review]
        data.unsupported = [record.model_dump(mode="json") for record in plan.unsupported]

        if not plan.actions and not plan.has_blocking_findings():
            state = "already_converged" if round_index == 0 else "converged"
            break
        if plan.has_blocking_findings():
            state = "manual_intervention_required"
            break
        if previous_fingerprint is not None and plan.drift_fingerprint == previous_fingerprint:
            state = "non_converged"
            errors = [EnvelopeError(code="no_progress", message="drift fingerprint did not change between rounds")]
            break
        previous_fingerprint = plan.drift_fingerprint

        op.emit("round_started", f"reconcile round {round_index} started", round=round_index)
        try:
            round_summary = _execute_round(
                cfg, op, artifacts, round_index, plan, snapshot, now, command_runner, interrupted
            )
        except _Interrupted:
            state = "failed"
            errors = [EnvelopeError(code="interrupted", message="reconcile was interrupted during action execution")]
            break
        except (ConfigError, NautobotError) as exc:
            state = "failed"
            errors = [EnvelopeError(code="reconcile_round_failed", message=str(exc))]
            break
        data.rounds.append(round_summary)
    else:
        state = "non_converged"
        errors = [EnvelopeError(code="max_rounds_reached", message=f"stopped after {rounds_limit} round(s)")]

    if final_drift_result is not None:
        final_data = render_drift_data(final_drift_result, final_generated_at, final_snapshot)
        data.final_drift_path = str(
            artifacts.write_json(
                f"round-{max(len(data.rounds) - 1, 0):02d}/drift-final.json", final_data.model_dump(mode="json")
            )
        )
        data.summary = final_data.summary
        data.scope_summary = _scope_summary(final_drift_result.targets, scope, final_snapshot)
        data.progress_made = bool(data.rounds)
        _write_dashboard(cfg, op, data, final_data)

    return _finish(op, data, state, errors)


def _execute_round(
    cfg: Config,
    op: OperationLog,
    artifacts: OperationArtifacts,
    round_index: int,
    plan: ReconcilePlan,
    snapshot: SourceSnapshot,
    now: Callable[[], datetime],
    command_runner: CommandRunner | None,
    interrupted: "_InterruptFlag",
) -> RoundSummary:
    summary = RoundSummary(round=round_index, drift_fingerprint=plan.drift_fingerprint)
    bootstrap_actions = [a for a in plan.actions if a.reconciler_id in _BOOTSTRAP_LEDGER_RECONCILERS]
    service_actions = [a for a in plan.actions if a.reconciler_id not in _BOOTSTRAP_LEDGER_RECONCILERS]

    client = NautobotClient(cfg.nautobot.url, cfg.nautobot.resolve_token())
    try:
        for action in bootstrap_actions:
            if interrupted.is_set():
                raise _Interrupted()
            summary.actions.append(
                _execute_action(cfg, op, artifacts, round_index, action, snapshot, client, now, command_runner)
            )
    finally:
        client.close()

    summary.actions.append(_regenerate_production_inventory(cfg))

    for action in service_actions:
        if interrupted.is_set():
            raise _Interrupted()
        summary.actions.append(
            _execute_action(cfg, op, artifacts, round_index, action, snapshot, None, now, command_runner)
        )

    observe_targets = sorted(
        {
            target.slug
            for action in plan.actions
            if action.requires_observation and action.reconciler_id != "observe_node"
            for target in action.targets
            if target.slug
        }
    )
    if observe_targets:
        summary.actions.append(
            _run_observation_action(
                cfg, op, artifacts, observe_targets, snapshot, now, command_runner, action_id="post_actuation_observation"
            )
        )

    return summary


def _regenerate_production_inventory(cfg: Config) -> ActionResult:
    playbook_dir = cfg.ansible.resolved_playbook_dir(cfg.source_path.parent)
    try:
        profiles, _digest = load_deployment_profiles(playbook_dir)
    except DeploymentProfilesError as exc:
        return ActionResult(
            action_id="regenerate_production_inventory",
            reconciler_id="production_inventory",
            action_kind="render",
            success=False,
            error=str(exc),
        )
    if not profiles:
        return ActionResult(
            action_id="regenerate_production_inventory",
            reconciler_id="production_inventory",
            action_kind="render",
            success=True,
            detail={"skipped": "no deployment profiles configured"},
        )
    envelope = build_production_render(cfg)
    write_error = write_production_artifacts(envelope, cfg.ansible.resolved_inventory(cfg.source_path.parent).parent)
    return ActionResult(
        action_id="regenerate_production_inventory",
        reconciler_id="production_inventory",
        action_kind="render",
        success=write_error is None,
        error=write_error.message if write_error is not None else None,
    )


def _execute_action(
    cfg: Config,
    op: OperationLog,
    artifacts: OperationArtifacts,
    round_index: int,
    action: ReconcileAction,
    snapshot: SourceSnapshot,
    client: NautobotClient | None,
    now: Callable[[], datetime],
    command_runner: CommandRunner | None,
) -> ActionResult:
    target_slugs = [t.slug for t in action.targets if t.slug]
    op.emit("action_started", f"action {action.id} started", action_id=action.id, reconciler_id=action.reconciler_id)

    try:
        if action.reconciler_id == "observe_node":
            return _run_observation_action(cfg, op, artifacts, target_slugs, snapshot, now, command_runner, action_id=action.id)
        if action.reconciler_id == "link_actual_node":
            assert client is not None
            link_result = execute_link_actual_node(client, action)
            return _actuation_result(op, action, target_slugs, True, link_result.model_dump(), requires_observation=False)
        if action.reconciler_id == "reconcile_ipam":
            assert client is not None
            job_runner = NautobotJobRunner(
                client,
                poll_interval_seconds=cfg.reconcile.job_poll_interval_seconds,
                timeout_seconds=cfg.reconcile.job_timeout_seconds,
                artifacts=artifacts,
                operation_log=op,
            )
            ipam_result = execute_reconcile_ipam(
                job_runner, action, artifact_relative_path=f"round-{round_index:02d}/jobs/ipam-{action.id}.json"
            )
            detail = {"conflicts": ipam_result.conflicts, "skipped": ipam_result.skipped}
            return _actuation_result(op, action, target_slugs, True, detail, requires_observation=False)
        if action.reconciler_id in ("service_profile", "dnsmasq_config"):
            return _run_playbook_action(cfg, op, artifacts, round_index, action, snapshot, command_runner)
        raise LedgerActionError("unknown_reconciler", f"no executor for reconciler {action.reconciler_id!r}")
    except (LedgerActionError, NautobotJobError, NautobotError) as exc:
        code = getattr(exc, "code", "action_failed")
        op.emit(
            "action_completed",
            f"action {action.id} failed",
            level="error",
            action_id=action.id,
            reconciler_id=action.reconciler_id,
            success=False,
            error=str(exc),
        )
        return ActionResult(
            action_id=action.id,
            reconciler_id=action.reconciler_id,
            action_kind=action.action_kind,
            target_slugs=target_slugs,
            success=False,
            error=f"{code}: {exc}",
        )


def _actuation_result(
    op: OperationLog,
    action: ReconcileAction,
    target_slugs: list[str],
    success: bool,
    detail: dict[str, Any],
    *,
    requires_observation: bool,
) -> ActionResult:
    op.emit(
        "action_completed",
        f"action {action.id} completed",
        action_id=action.id,
        reconciler_id=action.reconciler_id,
        success=success,
    )
    op.emit(
        "actuation_completed",
        f"actuation {action.id} completed",
        target_slugs=target_slugs,
        claimed_diff_codes=action.claimed_diff_codes,
        requires_observation=requires_observation,
        success=success,
    )
    return ActionResult(
        action_id=action.id,
        reconciler_id=action.reconciler_id,
        action_kind=action.action_kind,
        target_slugs=target_slugs,
        success=success,
        detail=detail,
    )


def _run_playbook_action(
    cfg: Config,
    op: OperationLog,
    artifacts: OperationArtifacts,
    round_index: int,
    action: ReconcileAction,
    snapshot: SourceSnapshot,
    command_runner: CommandRunner | None,
) -> ActionResult:
    target_slugs = [t.slug for t in action.targets if t.slug]
    if action.action_kind == "dnsmasq_config":
        envelope = build_dnsmasq_apply(cfg, apply_changes=True)
        if envelope.ok:
            return _actuation_result(op, action, target_slugs, True, {}, requires_observation=action.requires_observation)
        return _failed_action_result(
            op, action, target_slugs, {"errors": [e.model_dump() for e in envelope.errors]}, "dnsmasq apply failed"
        )

    host_slugs = sorted(action.parameters.get("host_slugs") or target_slugs)
    playbook_groups = _group_hosts_by_playbook(action, host_slugs, snapshot)
    playbook_dir = cfg.ansible.resolved_playbook_dir(cfg.source_path.parent)
    runner = AnsibleRunner(
        playbook_dir, timeout_seconds=cfg.reconcile.ansible_timeout_seconds, artifacts=artifacts, command_runner=command_runner
    )
    inventory = cfg.ansible.resolved_inventory(cfg.source_path.parent)

    all_ok = True
    detail: dict[str, Any] = {"runs": []}
    for rel_path, hosts in sorted(playbook_groups.items(), key=lambda item: item[0] or ""):
        if rel_path is None:
            all_ok = False
            detail["runs"].append({"error": f"no playbook resolved for hosts {hosts}"})
            continue
        playbook_path = playbook_dir / rel_path
        result = runner.run(
            ["ansible-playbook", "-i", str(inventory), str(playbook_path), "--limit", ",".join(hosts)],
            mode="apply",
            artifact_stem=f"round-{round_index:02d}/ansible/{action.id}-{Path(rel_path).stem}",
        )
        detail["runs"].append({"playbook": rel_path, "hosts": hosts, "exit_code": result.exit_code, "recap": result.recap})
        if result.exit_code != 0:
            all_ok = False

    if all_ok:
        return _actuation_result(op, action, target_slugs, True, detail, requires_observation=action.requires_observation)
    return _failed_action_result(op, action, target_slugs, detail, "one or more playbook runs failed")


def _failed_action_result(
    op: OperationLog, action: ReconcileAction, target_slugs: list[str], detail: dict[str, Any], message: str
) -> ActionResult:
    op.emit(
        "action_completed",
        f"action {action.id} failed",
        level="error",
        action_id=action.id,
        reconciler_id=action.reconciler_id,
        success=False,
    )
    return ActionResult(
        action_id=action.id,
        reconciler_id=action.reconciler_id,
        action_kind=action.action_kind,
        target_slugs=target_slugs,
        success=False,
        detail=detail,
        error=message,
    )


def _group_hosts_by_playbook(
    action: ReconcileAction, host_slugs: list[str], snapshot: SourceSnapshot
) -> dict[str | None, list[str]]:
    single = action.parameters.get("playbook")
    if single:
        return {single: host_slugs}

    playbook_by_os = action.parameters.get("playbook_by_os") or {}
    nodes_by_slug = {node.slug: node for node in snapshot.desired.nodes}
    configs_by_node_id = {oc.node_id: oc for oc in snapshot.desired.operational_configs}
    groups: dict[str | None, list[str]] = {}
    for slug in host_slugs:
        node = nodes_by_slug.get(slug)
        config = configs_by_node_id.get(node.id) if node else None
        os_name = (config.expected_host_os or config.declared_host_os) if config else None
        rel_path = playbook_by_os.get(os_name) if os_name else None
        groups.setdefault(rel_path, []).append(slug)
    return groups


def _run_observation_action(
    cfg: Config,
    op: OperationLog,
    artifacts: OperationArtifacts,
    target_slugs: list[str],
    snapshot: SourceSnapshot,
    now: Callable[[], datetime],
    command_runner: CommandRunner | None,
    *,
    action_id: str,
) -> ActionResult:
    try:
        result: ObservationResult = run_observation(
            cfg, snapshot.desired, target_slugs, artifacts, op, command_runner=command_runner, now=now()
        )
    except ValueError as exc:
        return ActionResult(
            action_id=action_id,
            reconciler_id="observe_node",
            action_kind="observation",
            target_slugs=target_slugs,
            success=False,
            error=str(exc),
        )
    return ActionResult(
        action_id=action_id,
        reconciler_id="observe_node",
        action_kind="observation",
        target_slugs=target_slugs,
        success=result.ok,
        detail={"hosts": [h.model_dump() for h in result.hosts]},
        error=result.error,
    )


def _write_dashboard(cfg: Config, op: OperationLog, data: ReconcileData, final_data: DriftData) -> None:
    drift_envelope = Envelope.build("nctl.drift.v1", final_data, [])
    try:
        dashboard_envelope = render_dashboard_from_drift(cfg, drift_envelope)
    except Exception as exc:  # dashboard/write-back failure must never overwrite the reconcile terminal reason
        op.emit("warning", f"dashboard regeneration failed: {exc}", level="warning")
        return
    data.dashboard = dashboard_envelope.data
    if not dashboard_envelope.ok:
        op.emit(
            "warning",
            "dashboard regeneration reported errors",
            level="warning",
            errors=[e.model_dump() for e in dashboard_envelope.errors],
        )


def _build_plan_or_error(
    cfg: Config,
    snapshot: SourceSnapshot,
    drift_result: DriftResult,
    scope: PlanScope,
    generated_at: str,
) -> tuple[ReconcilePlan, None] | tuple[None, EnvelopeError]:
    playbook_dir = cfg.ansible.resolved_playbook_dir(cfg.source_path.parent)
    try:
        profiles, _digest = load_deployment_profiles(playbook_dir)
        profile_names = set(profiles)
        reconciliation = load_profile_reconciliation(playbook_dir, profile_names)
    except (DeploymentProfilesError, ProfileReconciliationError):
        reconciliation = {}

    diffs = [d for target in drift_result.targets for d in target.diffs]
    try:
        plan = build_plan(
            snapshot=snapshot,
            diffs=diffs,
            scope=scope,
            drift_generated_at=generated_at,
            profile_reconciliation=reconciliation,
        )
    except HostScopeError as exc:
        return None, EnvelopeError(code="unknown_host", message=str(exc))
    except UnclassifiedDiffCodeError as exc:
        return None, EnvelopeError(code="unclassified_diff_code", message=str(exc))
    return plan, None


def _scope_summary(targets: list[TargetStatus], scope: PlanScope, snapshot: SourceSnapshot) -> dict[str, int]:
    if scope.kind == "cluster":
        selected = targets
    else:
        host_node = next((n for n in snapshot.desired.nodes if n.slug == scope.host_slug), None)
        if host_node is None:
            return {}
        service_ids = {
            p.service_id for p in snapshot.desired.placements if p.node_id == host_node.id and p.desired_state == "active"
        }
        services_by_slug = {s.slug: s for s in snapshot.desired.services}
        selected = [
            t
            for t in targets
            if t.target.kind == "global"
            or (t.target.kind == "node" and t.target.slug == host_node.slug)
            or (
                t.target.kind == "service"
                and services_by_slug.get(t.target.slug or "") is not None
                and services_by_slug[t.target.slug].id in service_ids
            )
        ]
    summary: dict[str, int] = {}
    for t in selected:
        summary[t.status.value] = summary.get(t.status.value, 0) + 1
    return summary


def _finish(op: OperationLog, data: ReconcileData, state: str, errors: list[EnvelopeError]) -> Envelope[ReconcileData]:
    """Build the terminal envelope with `ok` driven by the reconcile *state*.

    Unlike `Envelope.build` (`ok = not errors`), `manual_intervention_required`
    and `non_converged` are failures even though they carry no `EnvelopeError`
    -- the plan's `manual_review`/`unsupported` records are the reason, not a
    run-level error -- so `ok` is set explicitly from the exit-criteria state
    vocabulary instead.
    """

    data.state = state
    ok = state in ("planned", "already_converged", "converged")
    if state in ("already_converged", "converged"):
        op.emit("drift_resolved", "reconcile converged", state=state)
    elif state in ("manual_intervention_required", "non_converged"):
        op.emit("non_converged", "reconcile stopped without full convergence", level="warning", state=state)
    envelope = Envelope(schema=RECONCILE_SCHEMA, generated_at=datetime.now(timezone.utc), ok=ok, data=data, errors=errors)
    # `result.json` must exist before the `finished` event is visible: callers (`nctl ops
    # show`, the Phase 5 server) treat that event as the signal that the terminal envelope is
    # ready to read, so persisting after `op.finish()` would leave a real, observed window
    # where the operation shows "finished" but has no result yet.
    _persist_terminal_result(data.artifact_dir, envelope)
    op.finish(ok=ok, message=state)
    return envelope


def _persist_terminal_result(artifact_dir: str, envelope: Envelope[ReconcileData]) -> None:
    """Write the terminal envelope as a public `result.json`, matching the exit criterion that
    the artifact layout on disk is identical regardless of whether the CLI or the Phase 5 server
    triggered the run. Never fatal: a `result.json` write failure must not turn a completed
    reconcile into a reported failure.
    """

    if not artifact_dir:
        return
    try:
        artifacts = OperationArtifacts(Path(artifact_dir))
        path = artifacts.write_json("result.json", envelope.model_dump(mode="json", by_alias=True))
        path.chmod(0o644)
    except (ArtifactError, OSError):
        pass


def render_reconcile_text(envelope: Envelope[ReconcileData]) -> str:
    data = envelope.data
    lines = [
        f"operation_id: {data.operation_id}",
        f"mode: {data.mode}",
        f"scope: {data.scope.label()}",
        f"state: {data.state}",
        f"event_log: {data.event_log_path}",
    ]
    if data.plan_path:
        lines.append(f"plan: {data.plan_path}")
    if data.final_drift_path:
        lines.append(f"final_drift: {data.final_drift_path}")
    status_line = " ".join(f"{status}={count}" for status, count in sorted(data.scope_summary.items()))
    lines.append(f"scope summary: {status_line}" if status_line else "scope summary: (no targets)")
    if data.manual_review:
        lines.append(f"manual_review: {len(data.manual_review)} finding(s)")
    if data.unsupported:
        lines.append(f"unsupported: {len(data.unsupported)} finding(s)")
    for round_summary in data.rounds:
        lines.append(f"round {round_summary.round}: {len(round_summary.actions)} action(s)")
        for action in round_summary.actions:
            marker = "ok" if action.success else "FAILED"
            lines.append(f"    [{marker}] {action.action_id} ({action.reconciler_id})")
    for error in envelope.errors:
        lines.append(f"error [{error.code}]: {error.message}")
    lines.append(f"ok: {envelope.ok}")
    return "\n".join(lines)


class _InterruptFlag:
    """SIGINT/SIGTERM handling: mark interrupted, never start another action."""

    def __init__(self) -> None:
        self._set = False
        self._previous: dict[int, Any] = {}

    def is_set(self) -> bool:
        return self._set

    def _handle(self, signum: int, frame: Any) -> None:
        self._set = True

    def __enter__(self) -> "_InterruptFlag":
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self._previous[sig] = signal.signal(sig, self._handle)
            except (ValueError, OSError):
                pass  # not the main thread / unsupported platform: best-effort only
        return self

    def __exit__(self, *exc_info: object) -> None:
        for sig, handler in self._previous.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass
