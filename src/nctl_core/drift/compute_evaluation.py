"""Pure desired-compute to Proxmox-ledger comparison for VM realization P1.

This module derives candidates only.  It deliberately does not write links or
plan an action; Phase 2 owns recording an unambiguous candidate.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Iterable

from nctl_core.compute.contract import effective_compute_defaults, normalize_mac_address
from nctl_core.compute.model import DesiredComputeInstance, DesiredComputePlatform
from nctl_core.production.contract import actual_state_problem
from .context import DriftContext
from .model import DiffRecord, Severity, Target

if TYPE_CHECKING:
    from nctl_core.sources.snapshot import SourceSnapshot


def evaluate_compute(snapshot: SourceSnapshot, context: DriftContext) -> Iterable[DiffRecord]:
    """Yield realization diagnostics for all valid desired compute rows."""
    nodes = {node.id: node for node in snapshot.desired.nodes}
    platforms = {platform.id: platform for platform in snapshot.desired.compute_platforms}
    instances_by_platform: dict[str, list[DesiredComputeInstance]] = defaultdict(list)
    for instance in snapshot.desired.compute_instances:
        instances_by_platform[instance.platform_id].append(instance)

    yield from _source_issue_diffs(snapshot, nodes, platforms)
    for platform in snapshot.desired.compute_platforms:
        platform_target = Target(kind="compute_platform", slug=platform.slug, name=platform.name, id=platform.id)
        instances = instances_by_platform.get(platform.id, [])
        matched, failures = _match_platform(platform, snapshot, context)
        for code, message, desired, actual in failures:
            yield _diff(platform_target, code, Severity.ERROR, message, desired, actual)
            for instance in instances:
                yield _diff(_instance_target(instance, nodes), code, Severity.ERROR, message, desired, actual)
        yield _summary(platform_target, platform=platform, cluster=matched, instance=None, snapshot=snapshot)
        if failures:
            continue
        assert matched is not None
        for instance in instances:
            yield from _evaluate_instance(instance, platform, matched, nodes, snapshot)
        desired_node_ids = {instance.desired_node_id for instance in instances}
        desired_vmids = {instance.config.get("vmid") for instance in instances}
        for vm in snapshot.actual.virtual_machines:
            if vm.cluster_id != matched.id:
                continue
            if vm.proxmox and vm.proxmox.vmid in desired_vmids:
                continue
            if any(_normal_name(vm.name) == _normal_name(nodes[n].slug) for n in desired_node_ids if n in nodes):
                continue
            yield _diff(platform_target, "unexplained_compute_guest", Severity.INFO,
                        f"{platform.slug}: observed guest {vm.name!r} has no desired compute instance",
                        {}, {"virtual_machine_id": vm.id, "name": vm.name},)


def _source_issue_diffs(snapshot: SourceSnapshot, nodes: dict, platforms: dict) -> Iterable[DiffRecord]:
    instances = {instance.id: instance for instance in snapshot.desired.compute_instances}
    for issue in snapshot.desired.source_issues:
        severity = Severity.ERROR if issue.severity == "error" else Severity.WARNING
        if issue.target_kind == "compute_platform":
            target = Target(kind="compute_platform", slug=issue.target_slug_or_name, id=issue.target_id)
        elif issue.target_kind == "compute_instance":
            target = _instance_target(instances.get(issue.target_id), nodes)
        elif issue.target_kind == "endpoint":
            endpoint = next((e for e in snapshot.desired.endpoints if e.id == issue.target_id), None)
            target = Target(kind="node", slug=endpoint.node_slug if endpoint else issue.target_slug_or_name, id=endpoint.node_id if endpoint else None)
        else:
            target = Target(kind="global")
        yield _diff(target, issue.code, severity, issue.message, issue.evidence, {"scope": issue.scope})


def _match_platform(platform: DesiredComputePlatform, snapshot: SourceSnapshot, context: DriftContext):
    target = Target(kind="compute_platform", slug=platform.slug, name=platform.name, id=platform.id)
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
    problem = actual_state_problem(facts.observed_at if facts else None, context.generated_at)
    if problem:
        failures.append(("compute_platform_observation_stale", f"{platform.slug}: platform observation is not trustworthy", {}, {"reason": problem}))
    elif facts.observation_state != "complete":
        failures.append(("compute_platform_observation_stale", f"{platform.slug}: platform observation is incomplete", {}, {"reason": "platform_observation_incomplete"}))
    return cluster, failures


def _evaluate_instance(instance, platform, cluster, nodes, snapshot):
    target = _instance_target(instance, nodes)
    node = nodes.get(instance.desired_node_id)
    vms = [vm for vm in snapshot.actual.virtual_machines if vm.cluster_id == cluster.id]
    if instance.realized_vm_id:
        vm = next((candidate for candidate in snapshot.actual.virtual_machines if candidate.id == instance.realized_vm_id), None)
        if vm is None:
            yield _diff(target, "compute_realized_instance_missing", Severity.ERROR, f"{target.slug}: realized VM no longer exists", {}, {})
            yield _summary(target, platform, cluster, None, snapshot)
            return
        if vm.cluster_id != cluster.id:
            yield _diff(target, "compute_identity_conflict", Severity.ERROR, f"{target.slug}: realized VM belongs to another platform", {"dimension": "scope"}, {"cluster_id": vm.cluster_id})
            yield _summary(target, platform, cluster, vm, snapshot)
            return
        linked = True
    else:
        declared_vmid = instance.config.get("vmid")
        by_vmid = [candidate for candidate in vms if candidate.proxmox and candidate.proxmox.vmid == declared_vmid]
        if len(by_vmid) == 1:
            vm, basis, linked = by_vmid[0], "vmid", False
        else:
            by_name = [candidate for candidate in vms if node and _normal_name(candidate.name) == _normal_name(node.slug)]
            if len(by_name) == 1:
                vm, basis, linked = by_name[0], "name", False
            elif len(by_name) > 1:
                yield _diff(target, "compute_instance_candidate_ambiguous", Severity.ERROR, f"{target.slug}: multiple same-name VM candidates", {}, {"candidate_vm_ids": [v.id for v in by_name]})
                yield _summary(target, platform, cluster, None, snapshot)
                return
            else:
                yield _diff(target, "compute_instance_missing", Severity.ERROR, f"{target.slug}: no VM candidate exists in matched Cluster", {}, {"cluster_id": cluster.id})
                yield _summary(target, platform, cluster, None, snapshot)
                return
    facts = vm.proxmox
    actual_kind = {"lxc": "container", "qemu": "virtual_machine"}.get(facts.guest_type if facts else None)
    if actual_kind != instance.instance_kind:
        yield _diff(target, "compute_identity_conflict", Severity.ERROR, f"{target.slug}: guest kind conflicts", {"dimension": "kind", "instance_kind": instance.instance_kind}, {"guest_type": facts.guest_type if facts else None})
    if facts and facts.vmid != instance.config.get("vmid"):
        yield _diff(target, "compute_identity_conflict", Severity.ERROR, f"{target.slug}: VMID conflicts", {"dimension": "vmid", "vmid": instance.config.get("vmid")}, {"vmid": facts.vmid})
    if facts and facts.node not in (cluster.proxmox.observed_node_names if cluster.proxmox else []):
        yield _diff(target, "compute_identity_conflict", Severity.ERROR, f"{target.slug}: VM node is outside platform observation", {"dimension": "node"}, {"node": facts.node})
    if (facts.status if facts else None) != instance.desired_power_state:
        yield _diff(target, "compute_power_state_mismatch", Severity.WARNING, f"{target.slug}: desired power state differs", {"desired_power_state": instance.desired_power_state}, {"status": facts.status if facts else None})
    for field, actual in (("vcpus", vm.vcpus), ("memory_mb", vm.memory), ("root_disk_gb", (facts.lxc_rootfs.size_gb if facts and facts.lxc_rootfs else vm.disk))):
        expected = getattr(instance, field)
        if actual is not None and actual != expected:
            yield _diff(target, "compute_resource_mismatch", Severity.WARNING, f"{target.slug}: {field} differs", {field: expected}, {field: actual})
    if instance.instance_kind == "container" and facts and facts.lxc_rootfs and facts.lxc_rootfs.storage != instance.config.get("storage"):
        yield _diff(target, "compute_resource_mismatch", Severity.WARNING, f"{target.slug}: rootfs storage differs", {"storage": instance.config.get("storage")}, {"storage": facts.lxc_rootfs.storage})
    interfaces = [i for i in snapshot.actual.vm_interfaces if i.virtual_machine_id == vm.id and i.proxmox and i.proxmox.presence == "present"]
    if len(interfaces) == 1:
        iface = interfaces[0]
        endpoint = next((e for e in snapshot.desired.endpoints if node and e.node_id == node.id and e.endpoint_type == "primary"), None)
        if endpoint and normalize_mac_address(iface.mac_address) != endpoint.mac_address:
            yield _diff(target, "compute_endpoint_mac_conflict", Severity.ERROR, f"{target.slug}: endpoint MAC conflicts", {"mac_address": endpoint.mac_address}, {"mac_address": iface.mac_address})
        if iface.proxmox.bridge != instance.config.get("bridge"):
            yield _diff(target, "compute_resource_mismatch", Severity.WARNING, f"{target.slug}: bridge differs", {"bridge": instance.config.get("bridge")}, {"bridge": iface.proxmox.bridge})
    if not linked:
        yield _diff(target, "compute_instance_not_linked", Severity.WARNING, f"{target.slug}: VM matches but no ledger link is recorded", {}, {"virtual_machine_id": vm.id, "match_basis": basis})
    yield _summary(target, platform, cluster, vm, snapshot, match_basis=("linked" if linked else basis))


def _summary(target, platform, cluster, instance, snapshot, match_basis=None):
    defaults = {}
    if instance is not None:
        desired_instance = next((i for i in snapshot.desired.compute_instances if i.id == target.id), None)
        node_endpoints = [e for e in snapshot.desired.endpoints if desired_instance and e.node_id == desired_instance.desired_node_id]
        defaults = effective_compute_defaults(desired_instance, platform, node_endpoints) if desired_instance else {}
    return _diff(target, "compute_realization_summary", Severity.INFO, f"{target.slug}: compute realization summary", {"effective_defaults": defaults}, {"cluster_id": cluster.id if cluster else None, "virtual_machine_id": instance.id if instance else None, "match_basis": match_basis, "field_dispositions": {"template": "creation_only", "unprivileged": "unobservable"}})


def _instance_target(instance, nodes):
    if instance is None:
        return Target(kind="compute_instance")
    node = nodes.get(instance.desired_node_id)
    return Target(kind="compute_instance", slug=node.slug if node else None, name=node.name if node else None, id=instance.id)


def _normal_name(value):
    return (value or "").split(".", 1)[0].casefold()


def _diff(target, code, severity, message, desired, actual):
    return DiffRecord(target=target, code=code, severity=severity, message=message, desired=desired, actual=actual, sources=["desired", "actual"])
