"""`nctl actual`: read-only typed actual-state diagnostic (Phase 2 Step 6).

Renders a `devices` section (identity plus the allowlisted `ActualFacts`; with
`--detail` also the raw nodeutils facts stored in `inventory_raw_json`) and the
observer Device -> Proxmox Cluster -> guest graph nctl already fetches through
`nctl_core.sources.actual.fetch_actual_snapshot`. This is not drift: it has no
write path, no desired-side input, and it never invents the future desired Cluster slug
`aghub-pve` or infers desired ownership (plan.md Section 5.6). It is purely a typed view
of what `fetch_actual_snapshot` observed, plus any structured proxmox_* read errors.
"""

from __future__ import annotations

from typing import Any, Mapping

from pydantic import BaseModel

from nctl_core.config import Config, ConfigError
from nctl_core.nautobot import NautobotClient, NautobotError
from nctl_core.output import Envelope, EnvelopeError
from nctl_core.sources.actual import (
    ActualCluster,
    ActualDevice,
    ActualFacts,
    ActualSnapshot,
    ActualVirtualMachine,
    ActualVMInterface,
    ProxmoxFactsReadError,
    fetch_actual_snapshot,
)

_UNKNOWN_OBSERVER = "unknown"

ACTUAL_SCHEMA = "nctl.actual.v2"


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


class ActualDeviceData(BaseModel):
    """One realized Device: identity, the allowlisted facts, and (detail only) raw facts.

    `facts_raw` is the nodeutils `facts` dict nauto stored under the
    `inventory_raw_json` Device custom field, passed through unchanged. It is
    populated only when the caller asked for detail; deterministic processing
    (drift, planning) keeps consuming the allowlisted `facts` instead.
    """

    id: str
    name: str
    serial: str | None = None
    platform: str | None = None
    facts: ActualFacts
    facts_raw: dict[str, Any] | None = None


class ActualData(BaseModel):
    detail_level: str = "basic"
    devices: list[ActualDeviceData] = []
    clusters: list[ActualClusterData] = []
    read_errors: list[ProxmoxFactsReadError] = []


def build_actual(cfg: Config, *, detail: bool = False, host: str | None = None) -> Envelope[ActualData]:
    """Fetch the actual snapshot and render it as the `nctl.actual.v2` typed view."""

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

    data = render_actual_data(snapshot, detail=detail, host=host)
    errors = [
        EnvelopeError(
            code="proxmox_facts_invalid",
            message=f"{err.object_type} {err.object_id}: {err.field}: {err.message}",
            detail=err.model_dump(),
        )
        for err in snapshot.proxmox_read_errors
    ]
    if host is not None and not data.devices:
        errors.append(
            EnvelopeError(code="unknown_host", message=f"no Device named {host!r} in the actual snapshot")
        )
    return Envelope.build(ACTUAL_SCHEMA, data, errors)


def render_actual_data(
    snapshot: ActualSnapshot, *, detail: bool = False, host: str | None = None
) -> ActualData:
    """Build the typed `nctl.actual.v2` view from an already-fetched `ActualSnapshot`.

    Shared by `build_actual` (live fetch) and tests (fixture snapshots), mirroring the
    `build_*`/`render_*_data` split other `nctl_core` render modules already use.
    `host` scopes the devices section only; the cluster graph is not filtered.
    """

    devices = [
        _device_data(device, detail)
        for device in snapshot.devices
        if host is None or device.name == host
    ]
    devices.sort(key=lambda d: d.name)

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
    return ActualData(
        detail_level="raw" if detail else "basic",
        devices=devices,
        clusters=clusters,
        read_errors=snapshot.proxmox_read_errors,
    )


def _device_data(device: ActualDevice, detail: bool) -> ActualDeviceData:
    return ActualDeviceData(
        id=device.id,
        name=device.name,
        serial=device.serial,
        platform=device.platform,
        facts=device.actual_facts(),
        facts_raw=_raw_facts(device.facts) if detail else None,
    )


def _raw_facts(custom_fields: Mapping[str, Any]) -> dict[str, Any] | None:
    """The nodeutils `facts` dict at `inventory_raw_json.facts`, unchanged; None if absent."""

    raw = custom_fields.get("inventory_raw_json")
    if not isinstance(raw, Mapping):
        return None
    facts = raw.get("facts")
    return dict(facts) if isinstance(facts, Mapping) else None


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
    for device in envelope.data.devices:
        facts = device.facts
        lines.append(
            f"device {device.name}  system={facts.observed_system or '?'}  "
            f"ip={facts.local_ip or '?'}  collected {facts.collected_at or 'never'}"
        )
    if envelope.data.detail_level == "raw" and envelope.data.devices:
        lines.append("(raw per-device facts included in --json output only)")

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
