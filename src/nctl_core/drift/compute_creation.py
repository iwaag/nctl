"""Pure, pinned LXC-create derivation shared by drift, planning, and execution."""
from __future__ import annotations

from dataclasses import dataclass

from nctl_core.compute.contract import effective_lifecycle, select_compute_primary_endpoint
from nctl_core.drift.compute_realization import derive_compute_realizations


@dataclass(frozen=True)
class ComputeCreation:
    instance: object
    node: object
    platform: object
    cluster: object
    control_node: object
    parameters: dict
    failures: tuple[tuple[str, str, dict, dict], ...] = ()


def derive_compute_creations(snapshot, *, generated_at: str) -> dict[str, ComputeCreation]:
    nodes = {node.id: node for node in snapshot.desired.nodes}
    endpoints = {}
    for endpoint in snapshot.desired.endpoints:
        endpoints.setdefault(endpoint.node_id, []).append(endpoint)
    result = {}
    for instance_id, realization in derive_compute_realizations(snapshot, generated_at=generated_at).items():
        if realization.platform_failures or realization.virtual_machine is not None or realization.instance.realized_vm_id:
            continue
        if not any(code == "compute_instance_missing" for code, *_ in realization.instance_failures):
            continue
        node = nodes.get(realization.instance.desired_node_id)
        control = nodes.get(realization.platform.control_node_id)
        failures = []
        if node is None or control is None or effective_lifecycle(node.lifecycle, realization.platform.lifecycle) not in {"approved", "active"}:
            continue
        endpoint, endpoint_code = select_compute_primary_endpoint(endpoints.get(node.id, []))
        if endpoint_code:
            failures.append((endpoint_code, f"{node.slug}: no unique primary endpoint for compute creation", {}, {}))
        facts = realization.cluster.proxmox
        scopes = facts.storage_content if facts else {}
        template = realization.instance.config.get("template")
        template_ok = any(scope.state == "complete" and scope.content_type == "vztmpl" and any(item.volid == template for item in scope.items) for scope in scopes.values())
        if not template_ok:
            failures.append(("compute_template_unavailable", f"{node.slug}: template is unavailable in complete storage evidence", {"template": template}, {}))
        storage = realization.instance.config.get("storage")
        storage_ok = any(scope.storage == storage for scope in scopes.values()) or any(vm.proxmox and vm.proxmox.lxc_rootfs and vm.proxmox.lxc_rootfs.storage == storage for vm in snapshot.actual.virtual_machines if vm.cluster_id == realization.cluster.id)
        if not storage_ok:
            failures.append(("compute_storage_unavailable", f"{node.slug}: rootfs storage is not evidenced", {"storage": storage}, {}))
        bridge = realization.instance.config.get("bridge")
        if not any(iface.proxmox and iface.proxmox.bridge == bridge for iface in snapshot.actual.vm_interfaces):
            failures.append(("compute_bridge_unavailable", f"{node.slug}: bridge is not evidenced", {"bridge": bridge}, {}))
        vmid = realization.instance.config.get("vmid")
        if any(vm.proxmox and vm.proxmox.vmid == vmid for vm in snapshot.actual.virtual_machines if vm.cluster_id == realization.cluster.id):
            failures.append(("compute_vmid_conflict", f"{node.slug}: VMID is already observed", {"vmid": vmid}, {}))
        if endpoint and any(iface.mac_address and iface.mac_address.casefold() == endpoint.mac_address.casefold() for iface in [*snapshot.actual.interfaces, *snapshot.actual.vm_interfaces]):
            failures.append(("compute_endpoint_mac_conflict", f"{node.slug}: endpoint MAC is already observed", {"mac_address": endpoint.mac_address}, {}))
        if endpoint and any(ip.host == endpoint.ip_address for ip in snapshot.actual.ip_addresses):
            failures.append(("compute_endpoint_ip_conflict", f"{node.slug}: endpoint IP is already observed", {"ip_address": endpoint.ip_address}, {}))
        if not control.realized_device_id:
            failures.append(("compute_control_node_not_actionable", f"{control.slug}: control node is not actionable", {}, {}))
        params = {"host_slugs": [control.slug], "vmid": vmid, "template": template, "storage": storage, "bridge": bridge, "unprivileged": realization.instance.config.get("unprivileged"), "vcpus": realization.instance.vcpus, "memory_mb": realization.instance.memory_mb, "root_disk_gb": realization.instance.root_disk_gb, "hostname": node.slug, "mac_address": endpoint.mac_address if endpoint else None}
        result[instance_id] = ComputeCreation(realization.instance, node, realization.platform, realization.cluster, control, params, tuple(failures))
    return result
