"""GraphQL fetch layer for the actual-state source (Phase 2 Step 1).

`ActualFacts` and `read_actual_facts` define the closed allowlist of custom
fields the nauto `Ingest Nodeutils Inventory` Job writes onto a realized
Device. Nothing here infers a derived value (package manager, power policy,
service placement) from actual data.

Deviation from the ORM version, confirmed by introspecting the live schema
(2026-07-15): `host_system` and `network_interface` have no registered
`CustomField` definition, so Nautobot's GraphQL layer does not expose
`cf_host_system` / `cf_network_interface` shortcut fields (only
`cf_primary_mac_address`, `cf_primary_ip_address`, `cf_last_seen`, and
`cf_inventory_source` exist as shortcuts). All eight allowlisted fields are
therefore read from the raw `_custom_field_data` JSON instead, exactly as
nintent's `_device_custom_fields` did — `read_actual_facts` already expects a
plain mapping, so this needs no change to the ported function itself.

Step 4 addition: `devices.serial`/`devices.platform`, `interfaces.enabled`,
and `ip_addresses.dns_name` are pinned here (checked against the live schema,
2026-07-15) because the ported `drift/evaluation.py` node/endpoint matching
needs them — real Nautobot model fields nintent's ORM-based evaluator read
directly (`Device.serial`, `Device.platform`, `Interface.enabled`,
`IPAddress.dns_name`), not custom fields, so they were outside the original
allowlisted-facts query.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from nctl_core.nautobot import NautobotClient

ACTUAL_QUERY = """
{
  devices {
    id
    name
    serial
    platform { name }
    _custom_field_data
  }
  clusters {
    id
    name
    cluster_type { name }
    _custom_field_data
  }
  virtual_machines {
    id
    name
    cluster { id }
    status { name }
    role { name }
    vcpus
    memory
    disk
    _custom_field_data
  }
  vm_interfaces {
    id
    name
    mac_address
    virtual_machine { id }
    _custom_field_data
  }
  interfaces {
    id
    name
    mac_address
    enabled
    device { id }
  }
  ip_addresses {
    id
    host
    mask_length
    dns_name
    interfaces { id }
    vm_interfaces { id }
  }
}
"""

# Closed allowlist mapping each exportable actual fact to the dedicated custom
# field that the nauto nodeutils ingest job persists.  The exporter reads only
# these stable fields; adding a fact requires a concrete current consumer, a
# documented source path, and tests.
ACTUAL_FACT_FIELDS = {
    "observed_system": "host_system",
    "local_ip": "primary_ip_address",
    "mac_address": "primary_mac_address",
    "network_interface": "network_interface",
    "collected_at": "last_seen",
    "inventory_source": "inventory_source",
    "observed_services": "observed_services",
    "service_inventory_updated_at": "service_inventory_updated_at",
}


@dataclass(frozen=True)
class ActualFacts:
    """The closed set of observed facts exportable under schema 1.0.

    This structure has a field for each allowlisted fact and nothing else, so no
    derived operational value (package manager, power policy, service placement)
    can travel through it.
    """

    observed_system: str | None
    local_ip: str | None
    mac_address: str | None
    network_interface: str | None
    collected_at: str | None
    inventory_source: str | None
    observed_services: dict[str, dict[str, Any]] | None = None
    service_inventory_updated_at: str | None = None


def read_actual_facts(custom_fields: Mapping[str, Any] | None) -> ActualFacts:
    """Read only the allowlisted actual facts from a realized Device.

    Any key outside :data:`ACTUAL_FACT_FIELDS` is ignored, so raw inventory
    blobs and other observed payloads can never leak into the exported facts.
    """

    data = custom_fields or {}

    def field(name: str) -> str | None:
        value = data.get(ACTUAL_FACT_FIELDS[name])
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    return ActualFacts(
        observed_system=field("observed_system"),
        local_ip=field("local_ip"),
        mac_address=field("mac_address"),
        network_interface=field("network_interface"),
        collected_at=field("collected_at"),
        inventory_source=field("inventory_source"),
        observed_services=_observed_services(data.get(ACTUAL_FACT_FIELDS["observed_services"])),
        service_inventory_updated_at=field("service_inventory_updated_at"),
    )


def _observed_services(value: Any) -> dict[str, dict[str, Any]] | None:
    if not isinstance(value, Mapping):
        return None
    return {
        str(name): dict(entry)
        for name, entry in value.items()
        if name not in (None, "") and isinstance(entry, Mapping)
    }


class ActualDevice(BaseModel):
    id: str
    name: str
    serial: str | None = None
    platform: str | None = None
    facts: dict[str, Any] = {}

    def actual_facts(self) -> ActualFacts:
        return read_actual_facts(self.facts)


# --------------------------------------------------------------------------------------
# Proxmox typed actual state (Phase 2 Step 6).
#
# These models read only native Cluster/VirtualMachine/VMInterface fields plus the closed
# `proxmox_*` allowlist documented in plan.md Section 5.4/5.6 and produced by
# nauto/jobs/proxmox_upsert.py + proxmox_interfaces.py (verified against that source, not a
# live schema, since no live Nautobot was reachable in this sandbox -- see report2.6.md).
# Every nested model uses `extra="forbid"` so an unknown key inside a dedicated `proxmox_*`
# custom field is a structured read error, not a silent pass-through. Unrelated custom-field
# keys (e.g. `inventory_raw_json`, the Device fact allowlist above) are never read here --
# each `_build_*` function below picks only the documented `proxmox_*` keys out of
# `_custom_field_data` before handing them to these models.
# --------------------------------------------------------------------------------------


class ProxmoxObservationError(BaseModel):
    """One bounded closed-code error, as emitted by build_observation_detail()."""

    model_config = ConfigDict(extra="forbid")

    scope_kind: str | None = None
    scope_id: str | None = None
    section: str | None = None
    code: str | None = None


class ProxmoxObservationDetail(BaseModel):
    """`proxmox_observation_detail`: nauto's build_observation_detail() shape."""

    model_config = ConfigDict(extra="forbid")

    state: str
    omitted_error_count: int = 0
    errors: list[ProxmoxObservationError] = []


class ProxmoxLxcRootfs(BaseModel):
    """`proxmox_lxc_rootfs`: nauto's build_lxc_rootfs() shape. Never populated for QEMU."""

    model_config = ConfigDict(extra="forbid")

    storage: str | None = None
    volume: str | None = None
    size_gb: float | None = None


class ProxmoxInterfaceDiagnostic(BaseModel):
    """One bounded diagnostic entry inside `proxmox_interface_evidence[<slot>].diagnostics`."""

    model_config = ConfigDict(extra="forbid")

    config_slot: str | None = None
    guest_interface_name: str | None = None
    mac_address: str | None = None
    reason: str | None = None


class ProxmoxInterfaceEvidenceEntry(BaseModel):
    """One `proxmox_interface_evidence[<slot>]` entry (slot name or `"unmatched"`)."""

    model_config = ConfigDict(extra="forbid")

    evidence_observed_at: str | None = None
    diagnostics: list[ProxmoxInterfaceDiagnostic] = []


class ProxmoxManagedIpEntry(BaseModel):
    """One `proxmox_managed_ip_evidence["managed"][<address>/<prefix>]` entry."""

    model_config = ConfigDict(extra="forbid")

    ip_id: str | None = None
    evidence_observed_at: str | None = None


class ProxmoxManagedIpEvidence(BaseModel):
    """`proxmox_managed_ip_evidence`: only the ingestor-managed relation set (Section 5.5

    "Interface/IP convergence"). Native IP relations not present in `managed` remain visible
    on `ActualIPAddress.interface_ids`/future VM-interface relations but are not labeled here
    as fresh Proxmox-observed evidence -- callers must not infer freshness for them.
    """

    model_config = ConfigDict(extra="forbid")

    managed: dict[str, ProxmoxManagedIpEntry] = {}
    evidence_observed_at: str | None = None


class ProxmoxStorageContentItem(BaseModel):
    """One bounded item from a Cluster's ``proxmox_storage_content`` ledger."""

    model_config = ConfigDict(extra="forbid")

    volid: str
    content: str | None = None
    format: str | None = None
    size_bytes: int | None = None


class ProxmoxStorageScope(BaseModel):
    """One node/storage/content-type scope from the Cluster storage ledger."""

    model_config = ConfigDict(extra="forbid")

    node: str
    storage: str
    content_type: str
    state: str
    last_attempted_at: str | None = None
    evidence_observed_at: str | None = None
    omitted_error_count: int = 0
    errors: list[dict[str, Any]] = []
    items: list[ProxmoxStorageContentItem] = []


class ProxmoxClusterFacts(BaseModel):
    """The closed `proxmox_*` allowlist read from a Cluster's `_custom_field_data`."""

    model_config = ConfigDict(extra="forbid")

    scope_key: str | None = None
    identity_source: str | None = None
    observer_device_id: str | None = None
    observed_node_names: list[str] = []
    node_count: int | None = None
    observed_at: str | None = None
    observation_state: str | None = None
    observation_detail: ProxmoxObservationDetail | None = None
    storage_content: dict[str, ProxmoxStorageScope] = {}
    storage_content_invalid_scope_count: int = 0

    @model_validator(mode="before")
    @classmethod
    def _drop_invalid_storage_scopes(cls, value: Any) -> Any:
        """Keep valid scopes even if one independently collected scope is malformed.

        Storage scopes are independent evidence.  A malformed row must not erase the
        healthy Cluster platform facts and turn it into a misleading
        ``compute_platform_missing`` result.
        """
        if not isinstance(value, dict) or "storage_content" not in value:
            return value
        raw_content = value.get("storage_content")
        if not isinstance(raw_content, dict):
            result = dict(value)
            result["storage_content"] = {}
            result["storage_content_invalid_scope_count"] = 1
            return result
        valid: dict[str, ProxmoxStorageScope] = {}
        invalid_count = 0
        for key, raw_scope in raw_content.items():
            try:
                valid[str(key)] = ProxmoxStorageScope.model_validate(raw_scope)
            except ValidationError:
                invalid_count += 1
        result = dict(value)
        result["storage_content"] = valid
        result["storage_content_invalid_scope_count"] = invalid_count
        return result


class ProxmoxVirtualMachineFacts(BaseModel):
    """The closed `proxmox_*` allowlist read from a VirtualMachine's `_custom_field_data`."""

    model_config = ConfigDict(extra="forbid")

    guest_type: str | None = None
    vmid: int | None = None
    node: str | None = None
    status: str | None = None
    observed_at: str | None = None
    observation_state: str | None = None
    observation_detail: ProxmoxObservationDetail | None = None
    lxc_rootfs: ProxmoxLxcRootfs | None = None
    interface_evidence: dict[str, ProxmoxInterfaceEvidenceEntry] = {}


class ProxmoxVMInterfaceFacts(BaseModel):
    """The closed `proxmox_*` allowlist read from a VMInterface's `_custom_field_data`."""

    model_config = ConfigDict(extra="forbid")

    config_slot: str | None = None
    guest_interface_name: str | None = None
    bridge: str | None = None
    interface_source: str | None = None
    observed_at: str | None = None
    presence: str | None = None
    managed_ip_evidence: ProxmoxManagedIpEvidence | None = None


class ProxmoxFactsReadError(BaseModel):
    """A structured read error for one malformed dedicated `proxmox_*` custom field.

    Produced when a `proxmox_*` value fails its strict nested model (Section 5.6: "Report
    malformed dedicated JSON ... as a structured read error, not a silent skip or crash").
    The owning object is still returned with `proxmox is None`; this error travels alongside
    it in `ActualSnapshot.proxmox_read_errors` instead of raising out of the fetch.
    """

    model_config = ConfigDict(extra="forbid")

    object_type: str
    object_id: str
    field: str
    message: str


# Closed allowlist: only these `proxmox_*` custom-field keys are ever read for each object
# type. Anything else in `_custom_field_data` (including `inventory_raw_json` and unrelated
# fields) is never inspected.
_CLUSTER_PROXMOX_FIELDS = {
    "scope_key": "proxmox_scope_key",
    "identity_source": "proxmox_identity_source",
    "observer_device_id": "proxmox_observer_device_id",
    "observed_node_names": "proxmox_observed_node_names",
    "node_count": "proxmox_node_count",
    "observed_at": "proxmox_observed_at",
    "observation_state": "proxmox_observation_state",
    "observation_detail": "proxmox_observation_detail",
    "storage_content": "proxmox_storage_content",
}

_VM_PROXMOX_FIELDS = {
    "guest_type": "proxmox_guest_type",
    "vmid": "proxmox_vmid",
    "node": "proxmox_node",
    "status": "proxmox_status",
    "observed_at": "proxmox_observed_at",
    "observation_state": "proxmox_observation_state",
    "observation_detail": "proxmox_observation_detail",
    "lxc_rootfs": "proxmox_lxc_rootfs",
    "interface_evidence": "proxmox_interface_evidence",
}

_VMINTERFACE_PROXMOX_FIELDS = {
    "config_slot": "proxmox_config_slot",
    "guest_interface_name": "proxmox_guest_interface_name",
    "bridge": "proxmox_bridge",
    "interface_source": "proxmox_interface_source",
    "observed_at": "proxmox_observed_at",
    "presence": "proxmox_presence",
    "managed_ip_evidence": "proxmox_managed_ip_evidence",
}


def _select_allowlisted(custom_fields: Mapping[str, Any], allowlist: dict[str, str]) -> dict[str, Any]:
    """Copy only the allowlisted `proxmox_*` keys, dropping any key not in the mapping."""

    data = custom_fields or {}
    selected: dict[str, Any] = {}
    for model_field, cf_key in allowlist.items():
        if cf_key in data and data[cf_key] is not None:
            selected[model_field] = data[cf_key]
    return selected


def _read_proxmox_facts(
    *,
    custom_fields: Mapping[str, Any] | None,
    model: type[BaseModel],
    allowlist: dict[str, str],
    object_type: str,
    object_id: str,
    field_label: str,
    errors: list[ProxmoxFactsReadError],
) -> BaseModel | None:
    """Read+validate one object's allowlisted `proxmox_*` fields into a strict model.

    Returns ``None`` (and appends a bounded structured error) when the selected data fails
    the strict model -- an unrelated/unknown nested key never silently passes through.
    Returns ``None`` with no error when nothing allowlisted is present (host never observed).
    """

    data = custom_fields or {}
    selected = _select_allowlisted(data, allowlist)
    if not selected:
        return None
    try:
        return model.model_validate(selected)
    except ValidationError as exc:
        errors.append(
            ProxmoxFactsReadError(
                object_type=object_type, object_id=object_id, field=field_label, message=str(exc)
            )
        )
        return None


class ActualCluster(BaseModel):
    id: str
    name: str
    cluster_type: str | None = None
    proxmox: ProxmoxClusterFacts | None = None


class ActualVirtualMachine(BaseModel):
    id: str
    name: str
    cluster_id: str | None = None
    status: str | None = None
    role: str | None = None
    vcpus: int | None = None
    memory: int | None = None
    disk: int | None = None
    proxmox: ProxmoxVirtualMachineFacts | None = None


class ActualVMInterface(BaseModel):
    id: str
    name: str
    mac_address: str | None = None
    virtual_machine_id: str | None = None
    proxmox: ProxmoxVMInterfaceFacts | None = None


class ActualInterface(BaseModel):
    id: str
    name: str
    mac_address: str | None = None
    enabled: bool = True
    device_id: str | None = None


class ActualIPAddress(BaseModel):
    id: str
    host: str
    mask_length: int
    dns_name: str | None = None
    interface_ids: list[str] = []
    vm_interface_ids: list[str] = []


class ActualSnapshot(BaseModel):
    devices: list[ActualDevice] = []
    virtual_machines: list[ActualVirtualMachine] = []
    interfaces: list[ActualInterface] = []
    ip_addresses: list[ActualIPAddress] = []
    clusters: list[ActualCluster] = []
    vm_interfaces: list[ActualVMInterface] = []
    proxmox_read_errors: list[ProxmoxFactsReadError] = []


def fetch_actual_snapshot(client: NautobotClient) -> ActualSnapshot:
    data = client.graphql(ACTUAL_QUERY)
    read_errors: list[ProxmoxFactsReadError] = []
    snapshot = ActualSnapshot(
        devices=[_build_device(row) for row in data["devices"]],
        virtual_machines=[
            _build_virtual_machine(row, read_errors) for row in data["virtual_machines"]
        ],
        interfaces=[_build_interface(row) for row in data["interfaces"]],
        ip_addresses=[_build_ip_address(row) for row in data["ip_addresses"]],
        clusters=[_build_cluster(row, read_errors) for row in data.get("clusters", [])],
        vm_interfaces=[_build_vm_interface(row, read_errors) for row in data.get("vm_interfaces", [])],
        proxmox_read_errors=read_errors,
    )
    return snapshot


def _build_device(row: dict[str, Any]) -> ActualDevice:
    platform = row.get("platform")
    return ActualDevice(
        id=row["id"],
        name=row["name"],
        serial=row.get("serial") or None,
        platform=platform["name"] if platform else None,
        facts=row.get("_custom_field_data") or {},
    )


def _build_interface(row: dict[str, Any]) -> ActualInterface:
    device = row.get("device")
    return ActualInterface(
        id=row["id"],
        name=row["name"],
        mac_address=row.get("mac_address"),
        enabled=bool(row.get("enabled", True)),
        device_id=device["id"] if device else None,
    )


def _build_ip_address(row: dict[str, Any]) -> ActualIPAddress:
    return ActualIPAddress(
        id=row["id"],
        host=row["host"],
        mask_length=row["mask_length"],
        dns_name=row.get("dns_name"),
        interface_ids=[iface["id"] for iface in row.get("interfaces") or []],
        vm_interface_ids=[iface["id"] for iface in row.get("vm_interfaces") or []],
    )


def _build_cluster(row: dict[str, Any], errors: list[ProxmoxFactsReadError]) -> ActualCluster:
    cluster_type = row.get("cluster_type")
    proxmox = _read_proxmox_facts(
        custom_fields=row.get("_custom_field_data"),
        model=ProxmoxClusterFacts,
        allowlist=_CLUSTER_PROXMOX_FIELDS,
        object_type="cluster",
        object_id=row["id"],
        field_label="proxmox",
        errors=errors,
    )
    return ActualCluster(
        id=row["id"],
        name=row["name"],
        cluster_type=cluster_type["name"] if cluster_type else None,
        proxmox=proxmox,
    )


def _build_virtual_machine(row: dict[str, Any], errors: list[ProxmoxFactsReadError]) -> ActualVirtualMachine:
    cluster = row.get("cluster")
    status = row.get("status")
    role = row.get("role")
    proxmox = _read_proxmox_facts(
        custom_fields=row.get("_custom_field_data"),
        model=ProxmoxVirtualMachineFacts,
        allowlist=_VM_PROXMOX_FIELDS,
        object_type="virtual_machine",
        object_id=row["id"],
        field_label="proxmox",
        errors=errors,
    )
    return ActualVirtualMachine(
        id=row["id"],
        name=row["name"],
        cluster_id=cluster["id"] if cluster else None,
        status=status["name"] if status else None,
        role=role["name"] if role else None,
        vcpus=row.get("vcpus"),
        memory=row.get("memory"),
        disk=row.get("disk"),
        proxmox=proxmox,
    )


def _build_vm_interface(row: dict[str, Any], errors: list[ProxmoxFactsReadError]) -> ActualVMInterface:
    vm = row.get("virtual_machine")
    proxmox = _read_proxmox_facts(
        custom_fields=row.get("_custom_field_data"),
        model=ProxmoxVMInterfaceFacts,
        allowlist=_VMINTERFACE_PROXMOX_FIELDS,
        object_type="vm_interface",
        object_id=row["id"],
        field_label="proxmox",
        errors=errors,
    )
    return ActualVMInterface(
        id=row["id"],
        name=row["name"],
        mac_address=row.get("mac_address"),
        virtual_machine_id=vm["id"] if vm else None,
        proxmox=proxmox,
    )
