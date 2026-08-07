"""Pure, pinned guest-create (LXC/QEMU) derivation shared by drift, planning, and execution."""
from __future__ import annotations

from dataclasses import dataclass

from nctl_core.compute.contract import effective_lifecycle, select_compute_primary_endpoint, static_ipv4_network
from nctl_core.drift.compute_realization import derive_compute_realizations


# Mirrors compute_disposition._DESTROYABLE_GUEST_TYPES: an instance_kind
# outside this map must be rejected explicitly, never silently routed to LXC.
CREATABLE_GUEST_TYPES = {"container": "lxc", "virtual_machine": "qemu"}
# A QEMU guest boots an installer ISO; an LXC guest unpacks a container template.
TEMPLATE_CONTENT_TYPES = {"lxc": "vztmpl", "qemu": "iso"}


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
        guest_type = CREATABLE_GUEST_TYPES.get(realization.instance.instance_kind)
        if guest_type is None:
            failures.append(("compute_instance_kind_not_creatable", f"{node.slug}: instance_kind has no bounded creation path", {"instance_kind": realization.instance.instance_kind}, {}))
        endpoint, endpoint_code = select_compute_primary_endpoint(endpoints.get(node.id, []))
        if endpoint_code:
            failures.append((endpoint_code, f"{node.slug}: no unique primary endpoint for compute creation", {}, {}))
        network = static_ipv4_network(endpoint) if endpoint else None
        # Only `pct create` can inject the address; a QEMU guest receives its
        # address inside the guest (manual initial access), so the static
        # network gate is container-only.
        if guest_type == "lxc" and endpoint and network is None:
            failures.append(("compute_endpoint_network_incomplete", f"{node.slug}: static LXC creation requires an IPv4 CIDR and usable same-subnet gateway", {}, {}))
        facts = realization.cluster.proxmox
        scopes = facts.storage_content if facts else {}
        template = realization.instance.config.get("template")
        content_type = TEMPLATE_CONTENT_TYPES.get(guest_type)
        template_ok = any(scope.state == "complete" and scope.content_type == content_type and any(item.volid == template for item in scope.items) for scope in scopes.values())
        if guest_type is not None and not template_ok:
            failures.append(("compute_template_unavailable", f"{node.slug}: template is unavailable in complete {content_type} storage evidence", {"template": template}, {}))
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
        if endpoint and any(ip.host == endpoint.ip_address.split("/", 1)[0] for ip in snapshot.actual.ip_addresses):
            failures.append(("compute_endpoint_ip_conflict", f"{node.slug}: endpoint IP is already observed", {"ip_address": endpoint.ip_address}, {}))
        if not control.realized_device_id:
            failures.append(("compute_control_node_not_actionable", f"{control.slug}: control node is not actionable", {}, {}))
        params = {"guest_type": guest_type, "host_slugs": [control.slug], "vmid": vmid, "template": template, "storage": storage, "bridge": bridge, "vcpus": realization.instance.vcpus, "memory_mb": realization.instance.memory_mb, "root_disk_gb": realization.instance.root_disk_gb, "hostname": node.slug, "mac_address": endpoint.mac_address if endpoint else None}
        if guest_type == "lxc":
            params.update({"unprivileged": realization.instance.config.get("unprivileged"), "ipv4_cidr": network[0] if network else None, "gateway_ipv4": network[1] if network else None})
        result[instance_id] = ComputeCreation(realization.instance, node, realization.platform, realization.cluster, control, params, tuple(failures))
    return result
