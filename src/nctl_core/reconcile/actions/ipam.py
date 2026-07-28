"""IPAM Job action handler."""

from __future__ import annotations

from nctl_core.jobs import NautobotJobRunner
from nctl_core.reconcile.ledger import execute_reconcile_ipam

from ..model import ReconcileAction
from .contract import ActionContext, ExecutedAction, actuation_result


def execute(context: ActionContext, action: ReconcileAction) -> ExecutedAction:
    assert context.client is not None
    target_slugs = [target.slug for target in action.targets if target.slug]
    runner = NautobotJobRunner(context.client, poll_interval_seconds=context.cfg.reconcile.job_poll_interval_seconds, timeout_seconds=context.cfg.reconcile.job_timeout_seconds, artifacts=context.artifacts, operation_log=context.operation_log)
    ipam_result = execute_reconcile_ipam(runner, action, artifact_relative_path=f"round-{context.round_index:02d}/jobs/ipam-{action.id}.json")
    success = not ipam_result.unresolved_expected_endpoints
    detail = {"conflicts": ipam_result.conflicts, "skipped": ipam_result.skipped, "eligible_endpoint_ids": ipam_result.eligible_endpoint_ids, "applied_endpoint_ids": ipam_result.applied_endpoint_ids, "unresolved_expected_endpoints": ipam_result.unresolved_expected_endpoints}
    result = actuation_result(context, action, target_slugs, success, detail, requires_observation=False, mutated=bool(ipam_result.applied_endpoint_ids))
    if not success:
        result.error = f"reconcile_ipam: {len(ipam_result.unresolved_expected_endpoints)} expected endpoint(s) did not reach an applied/noop state"
    return ExecutedAction(result=result)
