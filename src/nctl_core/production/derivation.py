"""Pure derivation of one desired node's effective operational values.

This module owns the Phase 2 endpoint, platform, and override precedence.  It
does not read Nautobot, the wall clock, or mutable configuration; callers pass
the operation's fixed generation timestamp and the already allowlisted actual
facts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
from typing import Any, Mapping

from nctl_core.sources.actual import ActualFacts, actual_type_problem

from .contract import actual_state_problem


_OBSERVED_SYSTEM_MAP = {"Linux": "linux", "Darwin": "macos"}
_POWER_BY_PLATFORM = {
    "linux": frozenset({"none", "wol"}),
    "macos": frozenset({"none", "macos_sleep"}),
    "haos": frozenset({"none"}),
}


@dataclass(frozen=True)
class EndpointCandidate:
    id: str
    name: str
    endpoint_type: str
    node_slug: str
    ip_address: str | None = None
    dns_name: str | None = None
    mdns_name: str | None = None

    def usable_local(self) -> bool:
        return self.endpoint_type != "vpn" and bool(
            _normalized_ip(self.ip_address) or _text(self.dns_name) or _text(self.mdns_name)
        )

    def usable_vpn(self) -> bool:
        return self.endpoint_type == "vpn" and bool(_normalized_ip(self.ip_address))

    def address(self) -> str | None:
        return _normalized_ip(self.ip_address) or _text(self.dns_name) or _text(self.mdns_name)

    def evidence(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "endpoint_type": self.endpoint_type,
            "ip_address": self.ip_address,
            "dns_name": self.dns_name,
            "mdns_name": self.mdns_name,
        }


@dataclass(frozen=True)
class OperationalOverride:
    id: str
    declared_host_os: str | None = None
    connection_path: str | None = None
    local_endpoint_id: str | None = None
    tailscale_endpoint_id: str | None = None
    ansible_port: int | None = None
    power_control: str | None = None
    is_laptop: bool | None = None


@dataclass(frozen=True)
class ValueRecord:
    value: Any
    source: str
    source_reference: Mapping[str, Any]
    override_won: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "source": self.source,
            "source_reference": dict(self.source_reference),
            "override_won": self.override_won,
        }


@dataclass(frozen=True)
class EffectiveOperationalValues:
    actual_state_policy: ValueRecord
    host_os: ValueRecord
    connection_path: ValueRecord
    connection_endpoint: ValueRecord
    connection_address: ValueRecord
    ansible_port: ValueRecord
    power_control: ValueRecord
    is_laptop: ValueRecord
    selected_endpoint: EndpointCandidate

    def as_dict(self) -> dict[str, dict[str, Any]]:
        return {
            name: getattr(self, name).as_dict()
            for name in (
                "actual_state_policy",
                "host_os",
                "connection_path",
                "connection_endpoint",
                "connection_address",
                "ansible_port",
                "power_control",
                "is_laptop",
            )
        }


@dataclass(frozen=True)
class DerivationFailure(Exception):
    code: str
    message: str
    field: str
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message


def resolve_operational_values(
    *,
    node_id: str,
    node_slug: str,
    endpoints: tuple[EndpointCandidate, ...],
    override: OperationalOverride | None,
    realized_type: str | None,
    facts: ActualFacts | None,
    generated_at: str,
) -> EffectiveOperationalValues:
    """Resolve a complete value/provenance record or raise one local failure."""

    override_ref = lambda field_name: {
        "kind": "operational_override",
        "id": override.id if override else None,
        "field": field_name,
    }

    declared_host_os = override.declared_host_os if override else None
    if declared_host_os is not None:
        if declared_host_os != "haos":
            raise _failure(
                "unsupported_observed_host_os",
                "declared_host_os",
                f"unsupported declared host OS {declared_host_os!r}",
                {"declared_host_os": declared_host_os},
            )
        policy = ValueRecord(
            "declared", "derived", {"kind": "override_presence", "field": "declared_host_os"}, False
        )
        host_os = ValueRecord(declared_host_os, "override", override_ref("declared_host_os"), True)
    else:
        type_problem = actual_type_problem(realized_type)
        if type_problem:
            raise _failure(type_problem, "host_os", "a supported realized device is required", {})
        if facts is None:
            raise _failure("missing_actual_data", "host_os", "actual facts are missing", {})
        freshness_problem = actual_state_problem(facts.collected_at, generated_at)
        if freshness_problem:
            raise _failure(
                freshness_problem,
                "host_os",
                "actual observation is missing, stale, or invalid",
                {"collected_at": facts.collected_at},
            )
        if not facts.observed_system:
            raise _failure("missing_observed_system", "host_os", "observed system is missing", {})
        normalized_os = _OBSERVED_SYSTEM_MAP.get(facts.observed_system)
        if normalized_os is None:
            raise _failure(
                "unsupported_observed_host_os",
                "host_os",
                f"unsupported observed system {facts.observed_system!r}",
                {"observed_system": facts.observed_system},
            )
        policy = ValueRecord(
            "required", "derived", {"kind": "override_absence", "field": "declared_host_os"}, False
        )
        host_os = ValueRecord(
            normalized_os,
            "derived",
            {
                "kind": "nodeutils_observation",
                "observed_system": facts.observed_system,
                "collected_at": facts.collected_at,
            },
            False,
        )

    endpoint_by_id = {endpoint.id: endpoint for endpoint in endpoints}
    selected: EndpointCandidate
    forced_path = override.connection_path if override else None
    if forced_path == "tailscale":
        selected = _forced_endpoint(
            node_slug,
            endpoint_by_id,
            override.tailscale_endpoint_id if override else None,
            field_name="tailscale_endpoint",
            require_vpn=True,
        )
        path = ValueRecord("tailscale", "override", override_ref("connection_path"), True)
        endpoint_record = ValueRecord(
            selected.name, "override", {**override_ref("tailscale_endpoint"), "endpoint": selected.evidence()}, True
        )
    elif override and override.local_endpoint_id:
        selected = _forced_endpoint(
            node_slug,
            endpoint_by_id,
            override.local_endpoint_id,
            field_name="local_endpoint",
            require_vpn=False,
        )
        if forced_path not in (None, "local"):
            raise _failure(
                "unresolved_connection_path",
                "connection_path",
                "a forced local endpoint permits only the local path",
                {"connection_path": forced_path},
            )
        path = ValueRecord("local", "override", override_ref("local_endpoint"), True)
        endpoint_record = ValueRecord(
            selected.name, "override", {**override_ref("local_endpoint"), "endpoint": selected.evidence()}, True
        )
    else:
        if forced_path not in (None, "local"):
            raise _failure(
                "unresolved_connection_path",
                "connection_path",
                f"unsupported connection path {forced_path!r}",
                {"connection_path": forced_path},
            )
        selected = _derive_local_endpoint(node_slug, endpoints)
        if selected.node_slug != node_slug:
            raise _failure(
                "endpoint_node_mismatch",
                "connection_endpoint",
                f"endpoint belongs to {selected.node_slug!r}, not {node_slug!r}",
                {"endpoint": selected.evidence()},
            )
        endpoint_reference = {"kind": "desired_endpoint", **selected.evidence()}
        path = ValueRecord("local", "derived", endpoint_reference, False)
        endpoint_record = ValueRecord(selected.name, "derived", endpoint_reference, False)

    if path.value == "local" and policy.value == "required" and facts and _text(facts.local_ip):
        address = ValueRecord(
            _normalized_ip(facts.local_ip) or _text(facts.local_ip),
            "derived",
            {
                "kind": "nodeutils_observation",
                "field": "local_ip",
                "collected_at": facts.collected_at,
            },
            False,
        )
    else:
        selected_address = selected.address()
        if selected_address is None:
            raise _failure(
                "invalid_connection_address",
                "connection_address",
                "selected endpoint has no usable address",
                {"endpoint": selected.evidence()},
            )
        address = ValueRecord(
            selected_address,
            endpoint_record.source,
            {**dict(endpoint_record.source_reference), "address_field": _address_field(selected)},
            endpoint_record.override_won,
        )

    power_value = override.power_control if override and override.power_control is not None else "none"
    if power_value not in _POWER_BY_PLATFORM[host_os.value]:
        raise _failure(
            "invalid_platform_power",
            "power_control",
            f"power_control {power_value!r} is unsafe for {host_os.value!r}",
            {"host_os": host_os.value, "power_control": power_value},
        )

    return EffectiveOperationalValues(
        actual_state_policy=policy,
        host_os=host_os,
        connection_path=path,
        connection_endpoint=endpoint_record,
        connection_address=address,
        ansible_port=_optional_override_record(override, "ansible_port", override.ansible_port if override else None),
        power_control=_default_or_override_record(override, "power_control", power_value, "none"),
        is_laptop=_default_or_override_record(
            override, "is_laptop", override.is_laptop if override and override.is_laptop is not None else False, False
        ),
        selected_endpoint=selected,
    )


def _derive_local_endpoint(node_slug: str, endpoints: tuple[EndpointCandidate, ...]) -> EndpointCandidate:
    usable = sorted((endpoint for endpoint in endpoints if endpoint.usable_local()), key=lambda item: item.id)
    if len(usable) == 1:
        return usable[0]
    primaries = [endpoint for endpoint in usable if endpoint.endpoint_type == "primary"]
    if len(primaries) == 1:
        return primaries[0]
    evidence = {"node_slug": node_slug, "candidates": [endpoint.evidence() for endpoint in usable]}
    if not usable:
        evidence["endpoints"] = [endpoint.evidence() for endpoint in sorted(endpoints, key=lambda item: item.id)]
        raise _failure(
            "missing_connection_endpoint",
            "connection_endpoint",
            f"node {node_slug!r} has no usable local endpoint",
            evidence,
        )
    raise _failure(
        "ambiguous_connection_endpoints",
        "connection_endpoint",
        f"node {node_slug!r} has multiple usable endpoints and no unique primary",
        evidence,
    )


def _forced_endpoint(
    node_slug: str,
    endpoints: Mapping[str, EndpointCandidate],
    endpoint_id: str | None,
    *,
    field_name: str,
    require_vpn: bool,
) -> EndpointCandidate:
    endpoint = endpoints.get(endpoint_id or "")
    if endpoint is None:
        raise _failure(
            "unresolved_connection_path",
            field_name,
            f"forced {field_name} is missing from the node endpoint set",
            {"endpoint_id": endpoint_id},
        )
    if endpoint.node_slug != node_slug:
        raise _failure(
            "endpoint_node_mismatch",
            field_name,
            f"endpoint belongs to {endpoint.node_slug!r}, not {node_slug!r}",
            {"endpoint": endpoint.evidence()},
        )
    usable = endpoint.usable_vpn() if require_vpn else endpoint.usable_local()
    if not usable:
        raise _failure(
            "invalid_connection_address",
            field_name,
            "forced endpoint has no usable address for its path",
            {"endpoint": endpoint.evidence()},
        )
    return endpoint


def _optional_override_record(
    override: OperationalOverride | None, field_name: str, value: Any
) -> ValueRecord:
    if value is None:
        return ValueRecord(None, "default", {"kind": "ansible_default"}, False)
    return ValueRecord(
        value,
        "override",
        {"kind": "operational_override", "id": override.id if override else None, "field": field_name},
        True,
    )


def _default_or_override_record(
    override: OperationalOverride | None, field_name: str, value: Any, default: Any
) -> ValueRecord:
    explicitly_set = override is not None and getattr(override, field_name) is not None
    if not explicitly_set or value == default:
        return ValueRecord(value, "default", {"kind": "safe_default", "field": field_name}, False)
    return ValueRecord(
        value,
        "override",
        {"kind": "operational_override", "id": override.id, "field": field_name},
        True,
    )


def _address_field(endpoint: EndpointCandidate) -> str:
    for field_name in ("ip_address", "dns_name", "mdns_name"):
        if _text(getattr(endpoint, field_name)):
            return field_name
    raise AssertionError("address field requested for unusable endpoint")


def _failure(code: str, field_name: str, message: str, evidence: Mapping[str, Any]) -> DerivationFailure:
    return DerivationFailure(code=code, message=message, field=field_name, evidence=dict(evidence))


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalized_ip(value: Any) -> str | None:
    text = _text(value)
    if text is None:
        return None
    try:
        return str(ipaddress.ip_interface(text).ip)
    except ValueError:
        return None
