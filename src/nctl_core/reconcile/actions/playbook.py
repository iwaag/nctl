"""Generic service playbook action handler."""
from __future__ import annotations
from pathlib import Path
from nctl_core.ansible import AnsibleRunner
from nctl_core.production.adapter import build_production_node_inputs
from nctl_core.production.derivation import DerivationFailure, resolve_operational_values
from ..model import ReconcileAction
from .contract import ActionContext, ExecutedAction, actuation_result, failed_action_result

def _group_hosts_by_playbook(action, host_slugs, snapshot, *, generated_at):
    single=action.parameters.get("playbook")
    if single: return {single: host_slugs}
    by_os=action.parameters.get("playbook_by_os") or {}; inputs={node.slug: node for node in build_production_node_inputs(snapshot)}; groups={}
    for slug in host_slugs:
        node=inputs.get(slug)
        if node is None: raise ValueError(f"planned host {slug!r} is absent from the fixed source snapshot")
        try: effective=resolve_operational_values(node_id=node.id,node_slug=node.slug,endpoints=node.endpoints,override=node.operational_override,realized_type=node.realized.realized_type if node.realized else None,facts=node.realized.facts if node.realized else None,generated_at=generated_at)
        except DerivationFailure as exc: raise ValueError(f"planner invariant violated: host {slug!r} reached execution with {exc.code!r}") from exc
        rel=by_os.get(effective.host_os.value)
        if not rel: raise ValueError(f"no playbook is configured for derived host OS {effective.host_os.value!r} on {slug!r}")
        groups.setdefault(rel,[]).append(slug)
    return groups

def execute(context: ActionContext, action: ReconcileAction) -> ExecutedAction:
    target_slugs=[target.slug for target in action.targets if target.slug]; host_slugs=sorted(action.parameters.get("host_slugs") or target_slugs)
    groups=_group_hosts_by_playbook(action,host_slugs,context.snapshot,generated_at=context.generated_at); directory=context.cfg.ansible.resolved_playbook_dir(context.cfg.source_path.parent)
    runner=AnsibleRunner(directory,timeout_seconds=context.cfg.reconcile.ansible_timeout_seconds,artifacts=context.artifacts,command_runner=context.command_runner); inventory=context.cfg.ansible.resolved_inventory(context.cfg.source_path.parent); detail={"runs":[]}; all_ok=True
    for rel,hosts in sorted(groups.items()):
        result=runner.run(["ansible-playbook","-i",str(inventory),str(directory/rel),"--limit",",".join(hosts)],mode="apply",artifact_stem=f"round-{context.round_index:02d}/ansible/{action.id}-{Path(rel).stem}")
        detail["runs"].append({"playbook":rel,"hosts":hosts,"exit_code":result.exit_code,"recap":result.recap}); all_ok=all_ok and result.exit_code==0
    result=actuation_result(context,action,target_slugs,True,detail,requires_observation=action.requires_observation) if all_ok else failed_action_result(context,action,target_slugs,detail,"one or more playbook runs failed")
    return ExecutedAction(result=result)
