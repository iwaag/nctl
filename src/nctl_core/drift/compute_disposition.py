"""Retirement-aware, non-mutating compute disposition derivation.

This module is the sole interpreter of desired presence plus effective
lifecycle.  Both drift rendering and reconciliation planning consume its
typed result, rather than trying to recover intent from a diff message.
"""

from __future__ import annotations

from dataclasses import dataclass

from nctl_core.compute.contract import effective_lifecycle
from nctl_core.drift.compute_realization import ComputeRealization, derive_compute_realizations


@dataclass(frozen=True)
class ComputeDisposition:
    outcome: str
    realization: ComputeRealization | None
    parameters: dict | None = None
    gate_failure: str | None = None


def derive_compute_dispositions(snapshot, *, generated_at: str) -> dict[str, ComputeDisposition]:
    """Return exactly one retirement disposition for each desired instance."""
    nodes = {node.id: node for node in snapshot.desired.nodes}
    realizations = derive_compute_realizations(snapshot, generated_at=generated_at)
    result: dict[str, ComputeDisposition] = {}
    for instance in snapshot.desired.compute_instances:
        realization = realizations.get(instance.id)
        if realization is None or realization.platform_failures:
            result[instance.id] = ComputeDisposition("unknown", realization, gate_failure="platform_observation_untrustworthy")
            continue
        node = nodes.get(instance.desired_node_id)
        lifecycle = effective_lifecycle(node.lifecycle, realization.platform.lifecycle) if node else None
        if instance.desired_presence == "absent" and lifecycle != "retired":
            result[instance.id] = ComputeDisposition("presence_conflict", realization, gate_failure="desired_presence_requires_retired")
        elif lifecycle != "retired":
            result[instance.id] = ComputeDisposition("ordinary", realization)
        elif instance.desired_presence == "present":
            result[instance.id] = ComputeDisposition("retained", realization)
        elif realization.virtual_machine is not None and realization.virtual_machine.proxmox and realization.virtual_machine.proxmox.presence == "absent":
            result[instance.id] = ComputeDisposition("removal_complete", realization)
        else:
            parameters, failure = _destroy_parameters(realization, nodes)
            # `presence=None` is retained last-known evidence, not authorization
            # to destroy. It remains retained rather than guessed absent.
            result[instance.id] = ComputeDisposition(
                "destroy_required" if parameters else "retained", realization, parameters, failure
            )
    return result


def _destroy_parameters(realization: ComputeRealization, nodes: dict) -> tuple[dict | None, str | None]:
    instance, platform, cluster, vm = realization.instance, realization.platform, realization.cluster, realization.virtual_machine
    if realization.instance_failures or cluster is None or vm is None:
        return None, "realization_not_present_and_unambiguous"
    node = nodes.get(instance.desired_node_id)
    control = nodes.get(platform.control_node_id)
    facts = vm.proxmox
    if instance.instance_kind != "container":
        return None, "instance_kind_not_container"
    if facts is None or facts.presence != "present":
        return None, "guest_presence_not_explicitly_present"
    if facts.guest_type != "lxc":
        return None, "guest_type_not_lxc"
    if facts.vmid != instance.config.get("vmid"):
        return None, "vmid_disagrees"
    if vm.cluster_id != cluster.id:
        return None, "cluster_disagrees"
    if node is None or control is None:
        return None, "desired_node_or_control_node_missing"
    return {
        "compute_instance_id": instance.id,
        "desired_node_id": node.id,
        "desired_node_slug": node.slug,
        "compute_platform_id": platform.id,
        "compute_platform_slug": platform.slug,
        "cluster_id": cluster.id,
        "virtual_machine_id": vm.id,
        "guest_type": "lxc",
        "vmid": facts.vmid,
        "observed_proxmox_node": facts.node,
        "control_desired_node_id": control.id,
        "control_desired_node_slug": control.slug,
        "host_slugs": [control.slug],
    }, None
