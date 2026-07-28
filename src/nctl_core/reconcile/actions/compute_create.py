"""Bounded LXC creation handler; this is the sole Phase 3 Proxmox write seam."""
from __future__ import annotations
import json
from nctl_core.ansible import AnsibleRunner
from nctl_core.drift.compute_creation import derive_compute_creations
from ..model import ReconcileAction
from .contract import ActionContext, ExecutedAction, actuation_result, failed_action_result


def execute(context: ActionContext, action: ReconcileAction) -> ExecutedAction:
    target_slugs = [target.slug for target in action.targets if target.slug]
    creation = derive_compute_creations(context.snapshot, generated_at=context.generated_at).get(action.targets[0].id or "")
    if creation is None or creation.failures or creation.parameters != action.parameters:
        return ExecutedAction(result=failed_action_result(context, action, target_slugs, {}, "create parameters no longer match the pinned preflight"))
    result_path = context.artifacts.path(f"round-{context.round_index:02d}/compute/{action.id}.result.json")
    variables = {**action.parameters, "result_path": str(result_path)}
    directory = context.cfg.ansible.resolved_playbook_dir(context.cfg.source_path.parent)
    inventory = context.cfg.ansible.resolved_inventory(context.cfg.source_path.parent)
    runner = AnsibleRunner(directory, timeout_seconds=context.cfg.reconcile.ansible_timeout_seconds, artifacts=context.artifacts, command_runner=context.command_runner)
    command = ["ansible-playbook", "-i", str(inventory), str(directory / "playbooks/proxmox/create_lxc.yml"), "--limit", creation.control_node.slug, "--extra-vars", json.dumps(variables, sort_keys=True)]
    run = runner.run(command, mode="apply", artifact_stem=f"round-{context.round_index:02d}/ansible/{action.id}")
    detail = {"command": run.command, "exit_code": run.exit_code, "result_path": str(result_path)}
    if run.exit_code != 0:
        return ExecutedAction(result=failed_action_result(context, action, target_slugs, detail, "create playbook failed").model_copy(update={"mutated": True}))
    if not result_path.exists():
        return ExecutedAction(result=failed_action_result(context, action, target_slugs, detail, "create playbook did not write a result file").model_copy(update={"mutated": True}))
    try:
        payload = json.loads(result_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return ExecutedAction(result=failed_action_result(context, action, target_slugs, detail, f"invalid create result: {exc}").model_copy(update={"mutated": True}))
    if payload.get("created") is not True or payload.get("started") is not True:
        return ExecutedAction(result=failed_action_result(context, action, target_slugs, {**detail, "result": payload}, "create result did not confirm created and started").model_copy(update={"mutated": True}))
    return ExecutedAction(result=actuation_result(context, action, target_slugs, True, {**detail, "result": payload}, requires_observation=True, mutated=True))
