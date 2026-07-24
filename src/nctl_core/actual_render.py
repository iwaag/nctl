"""`nctl actual`: read-only typed actual-state diagnostic (Phase 2 Step 6).

Renders the observer Device -> Proxmox Cluster -> guest graph nctl already fetches
through `nctl_core.sources.actual.fetch_actual_snapshot`. This is not drift: it has no
write path, no desired-side input, and it never invents the future desired Cluster slug
`aghub-pve` or infers desired ownership (plan.md Section 5.6). It is purely a typed view
of what `fetch_actual_snapshot` observed, plus any structured proxmox_* read errors.
"""

from __future__ import annotations

from pydantic import BaseModel

from nctl_core.config import Config, ConfigError
from nctl_core.nautobot import NautobotClient, NautobotError
from nctl_core.output import Envelope, EnvelopeError
from nctl_core.sources.actual import (
    ActualCluster,
    ActualSnapshot,
    ActualVirtualMachine,
    ActualVMInterface,
    ProxmoxFactsReadError,
    fetch_actual_snapshot,
)

_UNKNOWN_OBSERVER = "unknown"

ACTUAL_SCHEMA = "nctl.actual.v1"


class ActualGuestData(BaseModel):
    id: str
    name: str
    guest_type: str | None = None
    vmid: int | None = None
    node: str | None = None
    status: str | None = None
    proxmox_status: str | None = None
    role: str | None = None
    vcpus: int | None = None
    memory: int | None = None
    disk: int | None = None
    observation_state: str | None = None
    observed_at: str | None = None
    rootfs: dict | None = None
    interfaces: list["ActualGuestInterfaceData"] = []


class ActualGuestInterfaceData(BaseModel):
    id: str
    config_slot: str | None = None
    guest_interface_name: str | None = None
    mac_address: str | None = None
    bridge: str | None = None
    interface_source: str | None = None
    presence: str | None = None
    observed_at: str | None = None
    managed_ip_count: int = 0
    unrelated_ip_ids: list[str] = []


class ActualClusterData(BaseModel):
    id: str
    name: str
    cluster_type: str | None = None
    identity_source: str | None = None
    scope_key: str | None = None
    observer_device_id: str | None = None
    observer_device_name: str | None = None
    observed_node_names: list[str] = []
    node_count: int | None = None
    observation_state: str | None = None
    observed_at: str | None = None
    guests: list[ActualGuestData] = []


class ActualData(BaseModel):
    clusters: list[ActualClusterData] = []
    read_errors: list[ProxmoxFactsReadError] = []


def build_actual(cfg: Config) -> Envelope[ActualData]:
    """Fetch the actual snapshot and render it as the `nctl.actual.v1` typed graph."""

    try:
        token = cfg.nautobot.resolve_token()
    except ConfigError as exc:
        return _failed(EnvelopeError(code="nautobot_token_error", message=str(exc)))

    client = NautobotClient(cfg.nautobot.url, token)
    try:
        snapshot = fetch_actual_snapshot(client)
    except NautobotError as exc:
        return _failed(EnvelopeError(code="nautobot_unreachable", message=str(exc)))
    finally:
        client.close()

    data = render_actual_data(snapshot)
    errors = [
        EnvelopeError(
            code="proxmox_facts_invalid",
            message=f"{err.object_type} {err.object_id}: {err.field}: {err.message}",
            detail=err.model_dump(),
        )
        for err in snapshot.proxmox_read_errors
    ]
    return Envelope.build(ACTUAL_SCHEMA, data, errors)


def render_actual_data(snapshot: ActualSnapshot) -> ActualData:
    """Build the typed `nctl.actual.v1` graph from an already-fetched `ActualSnapshot`.

    Shared by `build_actual` (live fetch) and tests (fixture snapshots), mirroring the
    `build_*`/`render_*_data` split other `nctl_core` render modules already use.
    """

    ifaces_by_vm: dict[str, list[ActualVMInterface]] = {}
    for iface in snapshot.vm_interfaces:
        if iface.virtual_machine_id:
            ifaces_by_vm.setdefault(iface.virtual_machine_id, []).append(iface)

    ip_relations_by_vm_iface: dict[str, list[str]] = {}
    for ip in snapshot.ip_addresses:
        for vm_iface_id in ip.vm_interface_ids:
            ip_relations_by_vm_iface.setdefault(vm_iface_id, []).append(ip.id)

    vms_by_cluster: dict[str, list[ActualVirtualMachine]] = {}
    for vm in snapshot.virtual_machines:
        if vm.cluster_id:
            vms_by_cluster.setdefault(vm.cluster_id, []).append(vm)

    device_names_by_id = {device.id: device.name for device in snapshot.devices}

    clusters = [
        _cluster_data(
            cluster, vms_by_cluster.get(cluster.id, []), ifaces_by_vm, ip_relations_by_vm_iface, device_names_by_id
        )
        for cluster in snapshot.clusters
    ]
    return ActualData(clusters=clusters, read_errors=snapshot.proxmox_read_errors)


def _cluster_data(
    cluster: ActualCluster,
    vms: list[ActualVirtualMachine],
    ifaces_by_vm: dict[str, list[ActualVMInterface]],
    ip_relations_by_vm_iface: dict[str, list[str]],
    device_names_by_id: dict[str, str],
) -> ActualClusterData:
    proxmox = cluster.proxmox
    observer_device_id = proxmox.observer_device_id if proxmox else None
    guests = [_guest_data(vm, ifaces_by_vm.get(vm.id, []), ip_relations_by_vm_iface) for vm in vms]
    guests.sort(key=lambda g: (g.guest_type or "", g.vmid if g.vmid is not None else -1))
    return ActualClusterData(
        id=cluster.id,
        name=cluster.name,
        cluster_type=cluster.cluster_type,
        identity_source=proxmox.identity_source if proxmox else None,
        scope_key=proxmox.scope_key if proxmox else None,
        observer_device_id=observer_device_id,
        observer_device_name=device_names_by_id.get(observer_device_id) if observer_device_id else None,
        observed_node_names=proxmox.observed_node_names if proxmox else [],
        node_count=proxmox.node_count if proxmox else None,
        observation_state=proxmox.observation_state if proxmox else None,
        observed_at=proxmox.observed_at if proxmox else None,
        guests=guests,
    )


def _guest_data(
    vm: ActualVirtualMachine,
    ifaces: list[ActualVMInterface],
    ip_relations_by_vm_iface: dict[str, list[str]],
) -> ActualGuestData:
    proxmox = vm.proxmox
    interface_rows = [_interface_data(iface, ip_relations_by_vm_iface) for iface in ifaces]
    interface_rows.sort(key=lambda i: i.config_slot or "")
    return ActualGuestData(
        id=vm.id,
        name=vm.name,
        guest_type=proxmox.guest_type if proxmox else None,
        vmid=proxmox.vmid if proxmox else None,
        node=proxmox.node if proxmox else None,
        status=vm.status,
        proxmox_status=proxmox.status if proxmox else None,
        role=vm.role,
        vcpus=vm.vcpus,
        memory=vm.memory,
        disk=vm.disk,
        observation_state=proxmox.observation_state if proxmox else None,
        observed_at=proxmox.observed_at if proxmox else None,
        rootfs=proxmox.lxc_rootfs.model_dump() if proxmox and proxmox.lxc_rootfs else None,
        interfaces=interface_rows,
    )


def _interface_data(
    iface: ActualVMInterface, ip_relations_by_vm_iface: dict[str, list[str]]
) -> ActualGuestInterfaceData:
    proxmox = iface.proxmox
    managed_ip_evidence = proxmox.managed_ip_evidence if proxmox else None
    managed_count = len(managed_ip_evidence.managed) if managed_ip_evidence else 0
    managed_ip_ids = {entry.ip_id for entry in (managed_ip_evidence.managed.values() if managed_ip_evidence else [])}
    unrelated_ip_ids = [
        ip_id for ip_id in ip_relations_by_vm_iface.get(iface.id, []) if ip_id not in managed_ip_ids
    ]
    return ActualGuestInterfaceData(
        id=iface.id,
        config_slot=proxmox.config_slot if proxmox else iface.name,
        guest_interface_name=proxmox.guest_interface_name if proxmox else None,
        mac_address=iface.mac_address,
        bridge=proxmox.bridge if proxmox else None,
        interface_source=proxmox.interface_source if proxmox else None,
        presence=proxmox.presence if proxmox else None,
        observed_at=proxmox.observed_at if proxmox else None,
        managed_ip_count=managed_count,
        unrelated_ip_ids=unrelated_ip_ids,
    )


def render_actual_text(envelope: Envelope[ActualData]) -> str:
    lines: list[str] = []
    observer_ids = sorted({c.observer_device_id for c in envelope.data.clusters if c.observer_device_id})
    if observer_ids:
        for observer_id in observer_ids:
            clusters = [c for c in envelope.data.clusters if c.observer_device_id == observer_id]
            observer_name = next((c.observer_device_name for c in clusters if c.observer_device_name), None)
            lines.append(f"observer {observer_name or observer_id}")
            for cluster in clusters:
                lines.extend(_render_cluster_lines(cluster))
    else:
        for cluster in envelope.data.clusters:
            lines.extend(_render_cluster_lines(cluster))

    if not envelope.data.clusters:
        lines.append("no actual Proxmox clusters observed")

    for err in envelope.errors:
        lines.append(f"error [{err.code}]: {err.message}")
    lines.append(f"ok: {envelope.ok}")
    return "\n".join(lines)


def _render_cluster_lines(cluster: ActualClusterData) -> list[str]:
    lines = [
        f"└─ cluster {cluster.name}  {cluster.observation_state or 'unknown'}  observed {cluster.observed_at or 'never'}"
    ]
    for idx, guest in enumerate(cluster.guests):
        branch = "└─" if idx == len(cluster.guests) - 1 else "├─"
        lines.append(
            f"   {branch} {guest.guest_type or '?'}  vmid={guest.vmid}  {guest.name}  "
            f"{guest.proxmox_status or guest.status or 'unknown'}  node={guest.node}"
        )
    return lines
