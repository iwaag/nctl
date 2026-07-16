"""GraphQL fetch layer for the actual-state source (Phase 2 Step 1).

`ActualFacts`/`read_actual_facts`/`actual_type_problem`/`missing_required_facts`
are ported unchanged from nintent's `actual_facts.py`: the closed allowlist of
custom fields the nauto `Ingest Nodeutils Inventory` Job writes onto a realized
Device. Nothing here infers a derived value (package manager, power policy,
service placement) from actual data — same guarantee as the original.

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
from typing import Any, Iterable, Mapping

from pydantic import BaseModel

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
  virtual_machines {
    id
    name
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
  }
}
"""

# The only realized object type schema 1.0 supports for actual-backed
# composition.  Realized Virtual Machines are skipped with
# ``unsupported_actual_type`` and deferred to a later schema.
SUPPORTED_REALIZED_TYPE = "device"

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

# Per-consumer required actual facts.  A fact is required only when a concrete
# current consumer needs it; not every allowlisted field is required on every
# host.
REQUIRED_FACT_BY_CONSUMER = {
    "host_os": "observed_system",  # observed OS selector groups and drift
    "wol": "mac_address",  # wake-on-LAN power control
    "network_interface": "network_interface",  # playbooks/profiles that bind to it
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


def actual_type_problem(realized_type: str | None) -> str | None:
    """Return a host-skip reason for an unusable realized actual type.

    ``None`` means the realized object is a Device and is eligible for
    actual-backed composition.
    """

    if not realized_type:
        return "no_realized_device"
    if realized_type == SUPPORTED_REALIZED_TYPE:
        return None
    return "unsupported_actual_type"


def missing_required_facts(facts: ActualFacts, consumers: Iterable[str]) -> list[str]:
    """Return skip reasons for consumer-specific facts that are absent.

    ``consumers`` lists which current consumers apply to this host (for example
    ``{"host_os", "wol"}``).  Only the facts those consumers need are required.
    """

    problems: list[str] = []
    for consumer in sorted(set(consumers)):
        try:
            attr = REQUIRED_FACT_BY_CONSUMER[consumer]
        except KeyError as exc:
            raise KeyError(f"unknown actual-fact consumer: {consumer!r}") from exc
        if getattr(facts, attr) is None:
            problems.append(f"missing_{attr}")
    return sorted(problems)


class ActualDevice(BaseModel):
    id: str
    name: str
    serial: str | None = None
    platform: str | None = None
    facts: dict[str, Any] = {}

    def actual_facts(self) -> ActualFacts:
        return read_actual_facts(self.facts)


class ActualVirtualMachine(BaseModel):
    id: str
    name: str


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


class ActualSnapshot(BaseModel):
    devices: list[ActualDevice] = []
    virtual_machines: list[ActualVirtualMachine] = []
    interfaces: list[ActualInterface] = []
    ip_addresses: list[ActualIPAddress] = []


def fetch_actual_snapshot(client: NautobotClient) -> ActualSnapshot:
    data = client.graphql(ACTUAL_QUERY)
    return ActualSnapshot(
        devices=[_build_device(row) for row in data["devices"]],
        virtual_machines=[
            ActualVirtualMachine(id=row["id"], name=row["name"]) for row in data["virtual_machines"]
        ],
        interfaces=[_build_interface(row) for row in data["interfaces"]],
        ip_addresses=[_build_ip_address(row) for row in data["ip_addresses"]],
    )


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
    )
