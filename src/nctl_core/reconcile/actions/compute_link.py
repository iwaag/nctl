"""Compute-realization ledger-link action handler."""

from nctl_core.drift.compute_realization import derive_compute_realizations
from nctl_core.reconcile.ledger import LedgerActionError, execute_link_compute_realization

from ..model import ReconcileAction
from .contract import ActionContext, ExecutedAction, actuation_result


def execute(context: ActionContext, action: ReconcileAction) -> ExecutedAction:
    assert context.client is not None
    realization = derive_compute_realizations(context.snapshot, generated_at=context.generated_at).get(action.targets[0].id or "")
    planned = action.parameters
    if realization is None or realization.cluster is None or realization.virtual_machine is None:
        raise LedgerActionError("compute_candidate_changed", "compute candidate is no longer uniquely derivable")
    actual = (realization.platform.id, realization.instance.id, realization.cluster.id, realization.virtual_machine.id, realization.match_basis)
    expected = (planned.get("compute_platform_id"), planned.get("compute_instance_id"), planned.get("cluster_id"), planned.get("virtual_machine_id"), planned.get("match_basis"))
    if actual != expected:
        raise LedgerActionError("compute_candidate_changed", "compute candidate changed between planning and execution", {"expected": expected, "actual": actual})
    result = execute_link_compute_realization(context.client, action)
    slugs = [target.slug for target in action.targets if target.slug]
    return ExecutedAction(result=actuation_result(context, action, slugs, True, result.model_dump(), requires_observation=False))
