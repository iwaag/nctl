"""Pure, typed realization derivation shared by compute drift and reconcile.

This is deliberately the only place that turns desired compute rows plus an
actual snapshot into a Cluster/VirtualMachine candidate.  Consumers render
the decision differently, but must never reconstruct it from a diff message.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from nctl_core.compute.model import DesiredComputeInstance, DesiredComputePlatform
from nctl_core.production.contract import actual_state_problem


@dataclass(frozen=True)
class ComputeRealization:
    instance: DesiredComputeInstance
    platform: DesiredComputePlatform
    cluster: Any | None
    virtual_machine: Any | None
    platform_failures: tuple[tuple[str, str, dict, dict], ...] = ()
    instance_failures: tuple[tuple[str, str, dict, dict], ...] = ()
    match_basis: str | None = None
    platform_link_state: str = "absent"
    instance_link_state: str = "absent"


def derive_compute_realizations(snapshot, *, generated_at: str) -> dict[str, ComputeRealization]:
    """Return one typed match decision for every desired compute instance."""
    nodes = {node.id: node for node in snapshot.desired.nodes}
    platforms = {platform.id: platform for platform in snapshot.desired.compute_platforms}
    result: dict[str, ComputeRealization] = {}
    for instance in snapshot.desired.compute_instances:
        platform = platforms.get(instance.platform_id)
        if platform is None:
            continue  # source-issue rendering owns malformed desired relations
        cluster, platform_failures = _match_platform(platform, snapshot, generated_at)
        platform_state = _link_state(platform.realized_cluster_id, cluster.id if cluster else None)
        if platform_failures or cluster is None:
            result[instance.id] = ComputeRealization(
                instance, platform, cluster, None, tuple(platform_failures),
                platform_link_state=platform_state,
            )
            continue
        vm, basis, failures = _match_instance(instance, platform, cluster, nodes, snapshot)
        instance_state = _link_state(instance.realized_vm_id, vm.id if vm else None)
        result[instance.id] = ComputeRealization(
            instance, platform, cluster, vm, tuple(platform_failures), tuple(failures), basis,
            platform_state, instance_state,
        )
    return result


def _link_state(linked_id: str | None, expected_id: str | None) -> str:
    if not linked_id:
        return "absent"
    return "linked_to_expected" if linked_id == expected_id else "linked_to_other"


def _match_platform(platform, snapshot, generated_at):
    clusters = {cluster.id: cluster for cluster in snapshot.actual.clusters}
    if platform.realized_cluster_id:
        cluster = clusters.get(platform.realized_cluster_id)
        if cluster is None:
            return None, [("compute_platform_missing", f"{platform.slug}: realized Cluster no longer exists", {}, {"reason": "realized_cluster_missing"})]
    else:
        control = next((node for node in snapshot.desired.nodes if node.id == platform.control_node_id), None)
        device_id = control.realized_device_id if control else None
        candidates = [c for c in snapshot.actual.clusters if c.proxmox and c.proxmox.observer_device_id == device_id]
        if not candidates:
            return None, [("compute_platform_missing", f"{platform.slug}: no Cluster observed for its control node", {}, {"control_node_id": platform.control_node_id})]
        if len(candidates) != 1:
            return None, [("compute_platform_ambiguous", f"{platform.slug}: multiple Clusters match its control node", {}, {"candidate_cluster_ids": [c.id for c in candidates]})]
        cluster = candidates[0]
    facts = cluster.proxmox
    failures = []
    declared = platform.config.get("cluster_name")
    if declared and cluster.name != declared:
        failures.append(("compute_identity_conflict", f"{platform.slug}: Cluster name disagrees with declared scope", {"dimension": "scope", "cluster_name": declared}, {"cluster_name": cluster.name}))
    problem = actual_state_problem(facts.observed_at if facts else None, generated_at)
    if problem:
        failures.append(("compute_platform_observation_stale", f"{platform.slug}: platform observation is not trustworthy", {}, {"reason": problem}))
    elif facts.observation_state != "complete":
        failures.append(("compute_platform_observation_stale", f"{platform.slug}: platform observation is incomplete", {}, {"reason": "platform_observation_incomplete"}))
    return cluster, failures


def _match_instance(instance, platform, cluster, nodes, snapshot):
    node = nodes.get(instance.desired_node_id)
    vms = [vm for vm in snapshot.actual.virtual_machines if vm.cluster_id == cluster.id]
    if instance.realized_vm_id:
        vm = next((candidate for candidate in snapshot.actual.virtual_machines if candidate.id == instance.realized_vm_id), None)
        if vm is None:
            return None, "linked", [("compute_realized_instance_missing", f"{node.slug if node else instance.id}: realized VM no longer exists", {}, {})]
        if vm.cluster_id != cluster.id:
            return vm, "linked", [("compute_identity_conflict", f"{node.slug if node else instance.id}: realized VM belongs to another platform", {"dimension": "scope"}, {"cluster_id": vm.cluster_id})]
        return vm, "linked", []
    declared_vmid = instance.config.get("vmid")
    by_vmid = [candidate for candidate in vms if candidate.proxmox and candidate.proxmox.vmid == declared_vmid]
    if len(by_vmid) == 1:
        return by_vmid[0], "vmid", []
    by_name = [candidate for candidate in vms if node and _normal_name(candidate.name) == _normal_name(node.slug)]
    if len(by_name) == 1:
        return by_name[0], "name", []
    if len(by_name) > 1:
        return None, None, [("compute_instance_candidate_ambiguous", f"{node.slug if node else instance.id}: multiple same-name VM candidates", {}, {"candidate_vm_ids": [v.id for v in by_name]})]
    return None, None, [("compute_instance_missing", f"{node.slug if node else instance.id}: no VM candidate exists in matched Cluster", {}, {"cluster_id": cluster.id})]


def _normal_name(value: str | None) -> str:
    return (value or "").split(".", 1)[0].casefold()
