"""dnsmasq deployment action handler."""
from __future__ import annotations
from nctl_core.dnsmasq_apply import build_dnsmasq_apply
from ..model import ReconcileAction
from .contract import ActionContext, ExecutedAction, actuation_result, failed_action_result

def execute(context: ActionContext, action: ReconcileAction) -> ExecutedAction:
    target_slugs = [target.slug for target in action.targets if target.slug]
    host_limit = sorted(action.parameters.get("host_slugs") or [])
    envelope = build_dnsmasq_apply(context.cfg, apply_changes=True, probe=context.ssh_probe, host_limit=host_limit)
    if envelope.ok:
        return ExecutedAction(result=actuation_result(context, action, target_slugs, True, {}, requires_observation=action.requires_observation))
    return ExecutedAction(result=failed_action_result(context, action, target_slugs, {"errors": [error.model_dump() for error in envelope.errors]}, "dnsmasq apply failed"))
