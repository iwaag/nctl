"""Observation action handler."""

from __future__ import annotations

from nctl_core.observation import ObservationResult, run_observation
from nctl_core.output import EnvelopeError
from nctl_core.ssh_enroll import SshStoreReadError

from ..model import ReconcileAction
from ..results import ActionResult
from .contract import ActionContext, ExecutedAction


def execute(context: ActionContext, action: ReconcileAction) -> ExecutedAction:
    target_slugs = [target.slug for target in action.targets if target.slug]
    try:
        result: ObservationResult = run_observation(
            context.cfg, context.snapshot.desired, target_slugs, context.artifacts, context.operation_log,
            command_runner=context.command_runner, now=context.now(),
        )
    except SshStoreReadError as exc:
        action_result = ActionResult(action_id=action.id, reconciler_id="observe_node", action_kind="observation", target_slugs=target_slugs, success=False, error=f"ssh_store_read_failed: {exc}")
        return ExecutedAction(result=action_result, terminal_errors=[EnvelopeError(code="ssh_store_read_failed", message=str(exc))])
    except ValueError as exc:
        return ExecutedAction(result=ActionResult(action_id=action.id, reconciler_id="observe_node", action_kind="observation", target_slugs=target_slugs, success=False, error=str(exc)))
    action_result = ActionResult(
        action_id=action.id,
        reconciler_id="observe_node",
        action_kind="observation",
        target_slugs=target_slugs,
        success=result.ok,
        detail={"nodeutils_version": result.nodeutils_version, "hosts": [host.model_dump() for host in result.hosts]},
        error=result.error,
    )
    # Ordinary drift-driven observation has historically been non-terminal so
    # independent work can still be assessed. An explicitly requested refresh
    # is different: the caller asked for fresh evidence, and a failed attempt
    # must never fall through to a stale-snapshot convergence result.
    if not result.ok and action.parameters.get("forced_refresh"):
        return ExecutedAction(
            result=action_result,
            terminal_errors=[
                EnvelopeError(
                    code="observation_failed",
                    message=result.error or "nodeutils observation did not complete successfully",
                )
            ],
        )
    return ExecutedAction(result=action_result)
