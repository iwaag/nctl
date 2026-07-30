"""Bounded, intent-pinned LXC destruction handler."""
from __future__ import annotations

import json

from nctl_core.ansible import AnsibleRunner
from nctl_core.drift.compute_disposition import derive_compute_dispositions

from ..model import ReconcileAction
from .contract import ActionContext, ExecutedAction, actuation_result, failed_action_result


def execute(context: ActionContext, action: ReconcileAction) -> ExecutedAction:
    """Destroy exactly the still-pinned LXC, returning controller-owned evidence."""
    target_slugs = [target.slug for target in action.targets if target.slug]
    disposition = derive_compute_dispositions(
        context.snapshot, generated_at=context.generated_at
    ).get(action.targets[0].id or "")
    if (
        disposition is None
        or disposition.outcome != "destroy_required"
        or disposition.parameters != action.parameters
    ):
        return ExecutedAction(result=failed_action_result(
            context, action, target_slugs, {},
            "destroy parameters no longer match the pinned disposition",
        ))

    result_path = context.artifacts.path(
        f"round-{context.round_index:02d}/compute/{action.id}.result.json"
    )
    # This must happen before the irreversible boundary so a post-command
    # failure is still recoverable from a controller-owned artifact location.
    result_path.parent.mkdir(parents=True, exist_ok=True)
    variables = {**action.parameters, "result_path": str(result_path)}
    directory = context.cfg.ansible.resolved_playbook_dir(context.cfg.source_path.parent)
    inventory = context.cfg.ansible.resolved_inventory(context.cfg.source_path.parent)
    control_host = action.parameters["control_desired_node_slug"]
    runner = AnsibleRunner(
        directory,
        timeout_seconds=context.cfg.reconcile.ansible_timeout_seconds,
        artifacts=context.artifacts,
        command_runner=context.command_runner,
    )
    command = [
        "ansible-playbook", "-i", str(inventory),
        str(directory / "playbooks/proxmox/destroy_lxc.yml"), "--limit", control_host,
        "--extra-vars", json.dumps(variables, sort_keys=True),
    ]
    run = runner.run(
        command, mode="apply", artifact_stem=f"round-{context.round_index:02d}/ansible/{action.id}"
    )
    detail = {"command": run.command, "exit_code": run.exit_code, "result_path": str(result_path)}
    if run.exit_code != 0:
        return ExecutedAction(result=failed_action_result(
            context, action, target_slugs, detail, "destroy playbook failed"
        ).model_copy(update={"mutated": True}))
    if not result_path.exists():
        return ExecutedAction(result=failed_action_result(
            context, action, target_slugs, detail, "destroy playbook did not write a result file"
        ).model_copy(update={"mutated": True}))
    try:
        payload = json.loads(result_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return ExecutedAction(result=failed_action_result(
            context, action, target_slugs, detail, f"invalid destroy result: {exc}"
        ).model_copy(update={"mutated": True}))
    if payload.get("absent") is not True:
        return ExecutedAction(result=failed_action_result(
            context, action, target_slugs, {**detail, "result": payload},
            "destroy result did not confirm absence",
        ).model_copy(update={"mutated": True}))
    destroyed = payload.get("destroyed") is True
    return ExecutedAction(result=actuation_result(
        context, action, target_slugs, True, {**detail, "result": payload},
        requires_observation=True, mutated=destroyed,
    ))
