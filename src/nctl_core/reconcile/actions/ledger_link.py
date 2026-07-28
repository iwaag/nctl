"""Desired-node ledger-link action handler."""

from __future__ import annotations

from nctl_core.reconcile.ledger import execute_link_actual_node

from ..model import ReconcileAction
from .contract import ActionContext, ExecutedAction, actuation_result


def execute(context: ActionContext, action: ReconcileAction) -> ExecutedAction:
    assert context.client is not None
    target_slugs = [target.slug for target in action.targets if target.slug]
    result = execute_link_actual_node(context.client, action)
    return ExecutedAction(result=actuation_result(context, action, target_slugs, True, result.model_dump(), requires_observation=False))
