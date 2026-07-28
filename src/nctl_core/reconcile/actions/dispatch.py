"""Static reconcile action dispatch and common error translation."""

from __future__ import annotations

from nctl_core.jobs import NautobotJobError
from nctl_core.nautobot import NautobotError
from nctl_core.reconcile.ledger import LedgerActionError

from ..model import ReconcileAction
from ..results import ActionResult
from . import ipam, ledger_link, observe
from .contract import ActionContext, ActionHandler, ExecutedAction


_HANDLERS = {
    "observe_node": ActionHandler("observe_node", observe.execute, "bootstrap", False),
    "link_actual_node": ActionHandler("link_actual_node", ledger_link.execute, "bootstrap", True),
    "reconcile_ipam": ActionHandler("reconcile_ipam", ipam.execute, "bootstrap", True),
}


def handler_for(reconciler_id: str) -> ActionHandler | None:
    return _HANDLERS.get(reconciler_id)


def action_phase(reconciler_id: str) -> str:
    handler = handler_for(reconciler_id)
    return handler.phase if handler is not None else "service"


def execute_action(context: ActionContext, action: ReconcileAction) -> ExecutedAction:
    target_slugs = [target.slug for target in action.targets if target.slug]
    context.operation_log.emit("action_started", f"action {action.id} started", action_id=action.id, reconciler_id=action.reconciler_id)
    handler = handler_for(action.reconciler_id)
    try:
        if handler is None:
            raise LedgerActionError("unknown_reconciler", f"no executor for reconciler {action.reconciler_id!r}")
        return handler.execute(context, action)
    except (LedgerActionError, NautobotJobError, NautobotError) as exc:
        code = getattr(exc, "code", "action_failed")
        mutated = bool(getattr(exc, "mutated", False))
        context.operation_log.emit("action_completed", f"action {action.id} failed", level="error", action_id=action.id, reconciler_id=action.reconciler_id, success=False, mutated=mutated, error=str(exc))
        return ExecutedAction(result=ActionResult(action_id=action.id, reconciler_id=action.reconciler_id, action_kind=action.action_kind, target_slugs=target_slugs, success=False, error=f"{code}: {exc}", mutated=mutated))
