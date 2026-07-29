"""`nctl reconcile`: the bounded plan/apply executor (Phase 4 Step 7).

Ties Step 5's planner and Step 6's ledger execution into one operation:
drift -> plan -> (plan mode stops here) -> execute actions in DAG order ->
fresh observation -> final drift. See `p4/plan.md`'s "Apply mode execution
per round" for the numbered steps this module implements.

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
from typing import TYPE_CHECKING, Any, Callable

from pydantic import BaseModel, Field

from nctl_core.artifacts import ArtifactError, OperationArtifacts
from nctl_core.config import Config, ConfigError
from nctl_core.drift.engine import DriftResult, TargetStatus
from nctl_core.drift.model import Target
from nctl_core.drift_render import DriftData, fetch_and_compute_drift, render_drift_data
from nctl_core.events import OperationLog
from nctl_core.nautobot import NautobotClient, NautobotError
from nctl_core.output import Envelope, EnvelopeError
from nctl_core.production.profiles import DeploymentProfilesError, load_deployment_profiles
from nctl_core.production_render import (
    ProductionRenderContext,
    build_production_render_context,
    write_production_artifacts,
)
from nctl_core.ssh_enroll import SshProbeRunner, SshStoreReadError, default_ssh_probe_runner
from nctl_core.sources.snapshot import SourceSnapshot, build_source_snapshot

from .classify import UnclassifiedDiffCodeError
from .lock import ReconcileLockError, acquire_reconcile_lock
from .model import PlanScope, ReconcileAction, ReconcilePlan
from .planner import HostScopeError, build_plan
from .profiles import ProfileReconciliationError, load_profile_reconciliation
from .results import ActionResult, RECONCILE_SCHEMA, ReconcileData, RoundSummary
from .actions.contract import ActionContext, ExecutedAction
from .actions.dispatch import action_phase, execute_action
from .ssh_preflight import (
    STATUS_MISMATCH,
    STATUS_READY,
    STATUS_UNENROLLED,
    STATUS_UNREACHABLE,
    SshPreflightEntry,
    action_host_slugs,
    check_ssh_enrollment,
    ssh_required_host_slugs,
    ssh_scan_errors,
    verify_offered_keys,
    verify_resolved_ssh_targets,
)

if TYPE_CHECKING:
    from nctl_core.ansible import CommandRunner

def _action_had_side_effects(result: ActionResult) -> bool:
    """Whether this result makes the round-start drift unsafe as final evidence."""
    return result.success or result.mutated




class RoundOutcome(BaseModel):
    """`_execute_round`'s always-returned result (fix_sshkey3 Step 2, contract item 6).

    `terminal_errors` non-empty means the round stops here, but `summary`
    still holds every `ActionResult` that actually ran before the stop --
    the caller always appends it to `data.rounds`, so a successful
    IPAM/bootstrap mutation is never silently dropped just because a later
    step in the same round (production regeneration, the post-regen SSH
    scan, a store read) failed. `had_side_effects` is true iff at least one
    appended action succeeded or positively recorded a mutation, and tells
    the caller whether a final read-only drift refresh is warranted before
    reporting `final_drift`.
    """

    summary: RoundSummary
    terminal_errors: list[EnvelopeError] = Field(default_factory=list)
    had_side_effects: bool = False




def run_reconcile(
    cfg: Config,
    *,
    host: str | None = None,
    apply_changes: bool = False,
    refresh_observation: bool = False,
    max_rounds: int | None = None,
    now: Callable[[], datetime] | None = None,
    command_runner: CommandRunner | None = None,
    operation_id: str | None = None,
    ssh_probe: SshProbeRunner | None = None,
) -> Envelope[ReconcileData]:
    now = now or (lambda: datetime.now(timezone.utc))
    ssh_probe = ssh_probe or default_ssh_probe_runner()
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

    if refresh_observation and scope.kind != "host":
        return _finish(
            op,
            data,
            "failed",
            [
                EnvelopeError(
                    code="refresh_observation_requires_host",
                    message="--refresh-observation requires one DesiredNode slug",
                )
            ],
        )

    if not apply_changes:
        return _run_plan_only(cfg, op, data, artifacts, scope, refresh_observation)

    try:
        with acquire_reconcile_lock(cfg.reconcile.resolved_lock_path()):
            with _InterruptFlag() as interrupted:
                return _run_apply(
                    cfg,
                    op,
                    data,
                    artifacts,
                    scope,
                    max_rounds,
                    now,
                    command_runner,
                    interrupted,
                    ssh_probe,
                    refresh_observation,
                )
    except ReconcileLockError as exc:
        return _finish(op, data, "failed", [EnvelopeError(code="reconcile_lock_contention", message=str(exc))])


def _run_plan_only(
    cfg: Config,
    op: OperationLog,
    data: ReconcileData,
    artifacts: OperationArtifacts,
    scope: PlanScope,
    refresh_observation: bool,
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
    if refresh_observation:
        plan = _with_forced_observation(plan, snapshot, scope)

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
    required = ssh_required_host_slugs(plan)
    if required:
        try:
            enrollment = check_ssh_enrollment(cfg, required, snapshot.desired)
        except SshStoreReadError as exc:
            return _finish(
                op, data, "failed", [EnvelopeError(code="ssh_store_read_failed", message=str(exc))]
            )
        data.ssh_preflight = [entry.model_dump() for entry in enrollment]
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
    ssh_probe: SshProbeRunner | None,
    refresh_observation: bool,
) -> Envelope[ReconcileData]:
    rounds_limit = max_rounds or cfg.reconcile.max_rounds
    previous_fingerprint: str | None = None
    final_drift_result: DriftResult | None = None
    final_generated_at = ""
    final_snapshot: SourceSnapshot | None = None
    final_state_unknown = False
    plan: ReconcilePlan | None = None
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
        if refresh_observation and round_index == 0:
            plan = _with_forced_observation(plan, snapshot, scope)
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
        # Decision 5 (better_usability p1): a global finding stops every
        # action immediately. A target-local finding blocks only its own
        # target -- if independent actions remain for other, healthy
        # targets, they still execute this round; only once no executable
        # action remains does a local finding also terminate the run.
        if plan.has_global_blocking_findings():
            state = "manual_intervention_required"
            break
        if not plan.actions and plan.has_local_blocking_findings():
            state = "manual_intervention_required"
            break
        if previous_fingerprint is not None and plan.drift_fingerprint == previous_fingerprint:
            state = "non_converged"
            errors = [EnvelopeError(code="no_progress", message="drift fingerprint did not change between rounds")]
            break
        previous_fingerprint = plan.drift_fingerprint

        # fix_sshkey Step 5 (Design Decision 5): a predictable missing enrollment
        # must block this round's writes before observation, Nautobot Jobs,
        # inventory writes, or playbooks run -- not surface only after they
        # already succeeded. Ledger-only actions on unrelated hosts are excluded
        # by ssh_required_host_slugs, so they are never blocked by this gate.
        required = ssh_required_host_slugs(plan)
        if required:
            try:
                enrollment = check_ssh_enrollment(cfg, required, snapshot.desired)
            except SshStoreReadError as exc:
                state = "failed"
                errors = [EnvelopeError(code="ssh_store_read_failed", message=str(exc))]
                break
            data.ssh_preflight = [entry.model_dump() for entry in enrollment]
            unenrolled = [entry for entry in enrollment if entry.status != STATUS_READY]
            if unenrolled:
                slugs = ", ".join(sorted(entry.slug for entry in unenrolled))
                state = "failed"
                errors = [
                    EnvelopeError(
                        code="ssh_host_key_unenrolled",
                        message=(
                            f"unenrolled SSH host(s): {slugs}; for each host, verify with "
                            "`nctl ssh enroll <slug> --from-known-hosts` (or an out-of-band "
                            "`--fingerprint`), then repeat with `--yes`"
                        ),
                        detail={"hosts": [entry.model_dump() for entry in unenrolled]},
                    )
                ]
                break

            # Presence in the trust file is not proof the current route offers
            # that key. Scan only observe_node targets over mDNS here -- the
            # bootstrap phase always connects that way. Service-phase targets
            # (service_profile/dnsmasq_config) are scanned again inside
            # _execute_round, after production regeneration, over whatever
            # route production actually resolved -- not mDNS, which a
            # service-phase host may not even answer on.
            scan_targets = ssh_required_host_slugs(plan, reconciler_ids=frozenset({"observe_node"}))
            if scan_targets:
                try:
                    verified = verify_offered_keys(cfg, scan_targets, snapshot.desired, ssh_probe)
                except SshStoreReadError as exc:
                    state = "failed"
                    errors = [EnvelopeError(code="ssh_store_read_failed", message=str(exc))]
                    break
                bad_errors = ssh_scan_errors(verified)
                if bad_errors:
                    state, errors = "failed", bad_errors
                    break

        op.emit("round_started", f"reconcile round {round_index} started", round=round_index)
        try:
            outcome = _execute_round(
                cfg, op, artifacts, round_index, plan, snapshot, now, command_runner, interrupted, ssh_probe
            )
        except (ConfigError, NautobotError) as exc:
            # Truly unexpected failures _execute_round itself cannot classify
            # (e.g. the bootstrap NautobotClient construction above its own
            # try/finally). No action ran yet in that case, so there is
            # nothing to append to data.rounds.
            state = "failed"
            errors = [EnvelopeError(code="reconcile_round_failed", message=str(exc))]
            break
        # fix_sshkey3 Step 2 (contract item 6): `outcome.summary` is appended
        # unconditionally -- interruption, an unavailable production
        # regeneration, a post-regen SSH scan failure, or a store-read
        # failure all still ran zero or more actions successfully first, and
        # that evidence must survive into `data.rounds` rather than being
        # discarded.
        data.rounds.append(outcome.summary)
        # A requested refresh is an evidence contract, not a best-effort hint.
        # Its action may report a normal (non-terminal) collection failure so
        # ordinary drift-driven observation can preserve its established
        # behavior, but this explicit first-round request must stop here rather
        # than allowing stale drift to be called converged in a later round.
        if refresh_observation and round_index == 0:
            forced_failure = next(
                (
                    action
                    for action in outcome.summary.actions
                    if action.reconciler_id == "observe_node" and not action.success
                ),
                None,
            )
            if forced_failure is not None:
                state = "failed"
                errors = [
                    EnvelopeError(
                        code="observation_failed",
                        message=forced_failure.error or "requested nodeutils observation did not complete successfully",
                    )
                ]
                break
        if outcome.terminal_errors:
            state = "failed"
            errors = outcome.terminal_errors
            if outcome.had_side_effects:
                # Item 7: a round-start drift snapshot must never be
                # mislabeled as final once an action succeeded or positively
                # recorded a mutation before full confirmation failed.
                refreshed = fetch_and_compute_drift(cfg)
                if isinstance(refreshed, EnvelopeError):
                    final_drift_result = None
                    final_state_unknown = True
                else:
                    final_snapshot, final_drift_result, final_generated_at = refreshed
            break
    else:
        if plan is not None and plan.has_local_blocking_findings() and not plan.has_global_blocking_findings():
            # The round limit landed exactly when a known local blocker was
            # the only thing left (independent progress just ran out on the
            # final permitted round) -- report the true, actionable reason
            # rather than the misleading "ran out of rounds".
            state = "manual_intervention_required"
        else:
            state = "non_converged"
            errors = [EnvelopeError(code="max_rounds_reached", message=f"stopped after {rounds_limit} round(s)")]

    if final_state_unknown:
        # Item 7: the refresh attempted after a failure-with-side-effects
        # itself failed -- report that final state is unknown instead of
        # silently keeping the stale round-start drift.
        errors = errors + [
            EnvelopeError(
                code="final_drift_unknown",
                message=(
                    "an action succeeded or a mutation was recorded before this round failed, "
                    "and the final drift refresh also failed"
                ),
            )
        ]
    elif final_drift_result is not None:
        final_data = render_drift_data(final_drift_result, final_generated_at, final_snapshot)
        data.final_drift_path = str(
            artifacts.write_json(
                f"round-{max(len(data.rounds) - 1, 0):02d}/drift-final.json", final_data.model_dump(mode="json")
            )
        )
        data.summary = final_data.summary
        data.scope_summary = _scope_summary(final_drift_result.targets, scope, final_snapshot)

    # Item 7: progress is whether any action in any round succeeded or
    # positively recorded a mutation, not merely whether a round's summary
    # was appended (an unenrolled/store-read failure before any action ran
    # also appends a summary with zero actions and must not count as progress).
    data.progress_made = any(
        action.success or action.mutated for round_summary in data.rounds for action in round_summary.actions
    )

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
    ssh_probe: SshProbeRunner,
) -> RoundOutcome:
    """Execute one round's actions and always return a `RoundOutcome`.

    fix_sshkey3 Step 2 (contract item 6): never raises for an expected
    terminal condition (interruption, unavailable production regeneration,
    a post-regeneration SSH scan failure, a managed-store read failure) --
    every one of those returns `summary` with whatever actions actually ran
    before the failure, so the caller can append it to `data.rounds` instead
    of discarding completed or partially-mutated evidence.
    `had_side_effects` tells the caller whether a final read-only drift
    refresh is warranted.
    """
    summary = RoundSummary(round=round_index, drift_fingerprint=plan.drift_fingerprint)
    operation_generated_at = plan.drift_generated_at or snapshot.fetched_at.isoformat()
    bootstrap_actions = [action for action in plan.actions if action_phase(action.reconciler_id) == "bootstrap"]
    service_actions = [action for action in plan.actions if action_phase(action.reconciler_id) == "service"]
    had_side_effects = False

    def _interrupted_outcome() -> RoundOutcome:
        return RoundOutcome(
            summary=summary,
            terminal_errors=[EnvelopeError(code="interrupted", message="reconcile was interrupted during action execution")],
            had_side_effects=had_side_effects,
        )

    client = NautobotClient(cfg.nautobot.url, cfg.nautobot.resolve_token())
    try:
        for action in bootstrap_actions:
            if interrupted.is_set():
                return _interrupted_outcome()
            executed = execute_action(
                ActionContext(
                    cfg=cfg, operation_log=op, artifacts=artifacts, round_index=round_index,
                    snapshot=snapshot, client=client, now=now, command_runner=command_runner,
                    ssh_probe=ssh_probe, generated_at=operation_generated_at,
                ),
                action,
            )
            summary.actions.append(executed.result)
            had_side_effects = had_side_effects or _action_had_side_effects(executed.result)
            if (
                action.reconciler_id == "observe_node"
                and action.parameters.get("forced_refresh")
                and not executed.result.success
            ):
                return RoundOutcome(
                    summary=summary,
                    terminal_errors=[
                        EnvelopeError(
                            code="observation_failed",
                            message=executed.result.error or "requested nodeutils observation did not complete successfully",
                        )
                    ],
                    had_side_effects=had_side_effects,
                )
            if executed.terminal_errors:
                return RoundOutcome(
                    summary=summary, terminal_errors=executed.terminal_errors, had_side_effects=had_side_effects
                )
    finally:
        client.close()

    regen_result, render_context = _regenerate_production_inventory(cfg)
    summary.actions.append(regen_result)
    had_side_effects = had_side_effects or _action_had_side_effects(regen_result)

    # fix_sshkey Step 5 (Design Decision 5) / fix_sshkey3 Step 2: a route
    # created only by an IPAM action just above may not have been testable
    # until now. Re-verify every service-phase SSH target against
    # `render_context.ssh_targets` -- the `ResolvedSshTarget` map this exact
    # regeneration just composed -- before the first production playbook
    # runs; presence in the trust store proved nothing about which key the
    # *newly selected* route actually offers, and the target map (never the
    # round-start `snapshot` above, which an IPAM/observation update earlier
    # in this same round can have already made stale) is the one source of
    # truth for route/port/generation.
    if render_context is None:
        if service_actions:
            return RoundOutcome(
                summary=summary,
                terminal_errors=[
                    EnvelopeError(
                        code="production_regeneration_unavailable",
                        message=(
                            regen_result.error
                            or "production inventory regeneration produced no usable render context; "
                            "no service action will run"
                        ),
                    )
                ],
                had_side_effects=had_side_effects,
            )
    else:
        service_scan_targets = ssh_required_host_slugs(
            plan, reconciler_ids=frozenset({"service_profile", "dnsmasq_config"})
        )
        if service_scan_targets:
            try:
                verified = verify_resolved_ssh_targets(
                    cfg, service_scan_targets, render_context.ssh_targets, ssh_probe, round_index=round_index
                )
            except SshStoreReadError as exc:
                return RoundOutcome(
                    summary=summary,
                    terminal_errors=[EnvelopeError(code="ssh_store_read_failed", message=str(exc))],
                    had_side_effects=had_side_effects,
                )
            summary.ssh_preflight = [entry.model_dump() for entry in verified]
            scan_errors = ssh_scan_errors(verified)
            if scan_errors:
                return RoundOutcome(summary=summary, terminal_errors=scan_errors, had_side_effects=had_side_effects)

        for action in service_actions:
            if interrupted.is_set():
                return _interrupted_outcome()
            executed = execute_action(
                ActionContext(cfg=cfg, operation_log=op, artifacts=artifacts, round_index=round_index,
                              snapshot=snapshot, client=None, now=now, command_runner=command_runner,
                              ssh_probe=ssh_probe, generated_at=operation_generated_at), action)
            summary.actions.append(executed.result)
            had_side_effects = had_side_effects or _action_had_side_effects(executed.result)
            if executed.terminal_errors:
                return RoundOutcome(
                    summary=summary, terminal_errors=executed.terminal_errors, had_side_effects=had_side_effects
                )

    observe_targets = sorted(
        {
            slug
            for action in plan.actions
            if action.requires_observation and action.reconciler_id != "observe_node"
            for slug in action_host_slugs(action)
        }
    )
    if observe_targets:
        observation_action = ReconcileAction(
            id="post_actuation_observation", reconciler_id="observe_node", action_kind="observation",
            targets=[Target(kind="node", slug=slug, name=slug) for slug in observe_targets],
            claimed_diff_codes=[], reason="Post-actuation observation.", mutates=True, requires_observation=False,
        )
        executed = execute_action(
            ActionContext(
                cfg=cfg, operation_log=op, artifacts=artifacts, round_index=round_index,
                snapshot=snapshot, client=None, now=now, command_runner=command_runner,
                ssh_probe=ssh_probe, generated_at=operation_generated_at,
            ),
            observation_action,
        )
        summary.actions.append(executed.result)
        had_side_effects = had_side_effects or _action_had_side_effects(executed.result)
        if executed.terminal_errors:
            return RoundOutcome(
                summary=summary, terminal_errors=executed.terminal_errors, had_side_effects=had_side_effects
            )

    return RoundOutcome(summary=summary, terminal_errors=[], had_side_effects=had_side_effects)


def _regenerate_production_inventory(cfg: Config) -> tuple[ActionResult, ProductionRenderContext | None]:
    """Regenerate the production inventory and return its render context alongside the action result.

    fix_sshkey3 Step 2 (previously fix_sshkey2 Step 3): the context is
    `None` whenever there is nothing a caller could safely resolve a
    same-generation `ResolvedSshTarget` from -- a deployment-profiles load
    failure, no profiles configured at all, or a failed render/atomic-
    install. Only a `None`-free context's `ssh_targets` map may be handed to
    `verify_resolved_ssh_targets` for post-regeneration scanning; every
    other outcome must stop before any service action runs (this function's
    caller enforces that).
    """
    playbook_dir = cfg.ansible.resolved_playbook_dir(cfg.source_path.parent)
    try:
        profiles, _digest = load_deployment_profiles(playbook_dir)
    except DeploymentProfilesError as exc:
        return (
            ActionResult(
                action_id="regenerate_production_inventory",
                reconciler_id="production_inventory",
                action_kind="render",
                success=False,
                error=str(exc),
            ),
            None,
        )
    if not profiles:
        return (
            ActionResult(
                action_id="regenerate_production_inventory",
                reconciler_id="production_inventory",
                action_kind="render",
                success=True,
                detail={"skipped": "no deployment profiles configured"},
            ),
            None,
        )

    try:
        token = cfg.nautobot.resolve_token()
    except ConfigError as exc:
        return (
            ActionResult(
                action_id="regenerate_production_inventory",
                reconciler_id="production_inventory",
                action_kind="render",
                success=False,
                error=str(exc),
            ),
            None,
        )
    client = NautobotClient(cfg.nautobot.url, token)
    try:
        fresh_snapshot = build_source_snapshot(cfg, client)
    except NautobotError as exc:
        return (
            ActionResult(
                action_id="regenerate_production_inventory",
                reconciler_id="production_inventory",
                action_kind="render",
                success=False,
                error=str(exc),
            ),
            None,
        )
    finally:
        client.close()

    render_context = build_production_render_context(cfg, fresh_snapshot)
    write_error = write_production_artifacts(
        render_context.envelope, cfg.ansible.resolved_inventory(cfg.source_path.parent).parent
    )
    success = write_error is None
    result = ActionResult(
        action_id="regenerate_production_inventory",
        reconciler_id="production_inventory",
        action_kind="render",
        success=success,
        error=write_error.message if write_error is not None else None,
    )
    return result, (render_context if success else None)




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


def _with_forced_observation(
    plan: ReconcilePlan,
    snapshot: SourceSnapshot,
    scope: PlanScope,
) -> ReconcilePlan:
    """Force one scoped node observation in the first requested round.

    A healthy drift snapshot normally has no observe_node action. Explicit
    refresh must still update the collector/report once, then let the next
    round's ordinary plan decide convergence without forcing another loop.
    """

    if scope.kind != "host" or not scope.host_slug:  # guarded by run_reconcile
        raise ValueError("forced observation requires host scope")
    node = next(n for n in snapshot.desired.nodes if n.slug == scope.host_slug)
    evidence = {"forced_refresh": True}
    actions = list(plan.actions)
    for index, action in enumerate(actions):
        if action.reconciler_id != "observe_node":
            continue
        if any(target.slug == node.slug for target in action.targets):
            actions[index] = action.model_copy(
                update={
                    "reason": "Explicit observation refresh requested; " + action.reason,
                    "evidence": {**action.evidence, **evidence},
                    "parameters": {**action.parameters, **evidence},
                }
            )
            return plan.model_copy(update={"actions": actions})

    forced = ReconcileAction(
        id="observe_node",
        reconciler_id="observe_node",
        action_kind="observation",
        targets=[Target(kind="node", slug=node.slug, name=node.name, id=node.id)],
        claimed_diff_codes=[],
        reason="Explicit observation refresh requested for this node.",
        evidence=evidence,
        parameters=evidence,
        mutates=True,
        requires_observation=False,
    )
    return plan.model_copy(update={"actions": [forced, *actions]})


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
            or (t.target.kind == "compute_instance" and t.target.slug == host_node.slug)
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
    # show`) treat that event as the signal that the terminal envelope is ready to read, so
    # persisting after `op.finish()` would leave a real, observed window where the operation
    # shows "finished" but has no result yet.
    _persist_terminal_result(data.artifact_dir, envelope)
    op.finish(ok=ok, message=state)
    return envelope


def _persist_terminal_result(artifact_dir: str, envelope: Envelope[ReconcileData]) -> None:
    """Write the terminal envelope as a public `result.json` for later `nctl ops show`
    inspection. Never fatal: a `result.json` write failure must not turn a completed reconcile
    into a reported failure.
    """

    if not artifact_dir:
        return
    try:
        artifacts = OperationArtifacts(Path(artifact_dir))
        path = artifacts.write_json("result.json", envelope.model_dump(mode="json", by_alias=True))
        path.chmod(0o644)
    except (ArtifactError, OSError):
        pass


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
