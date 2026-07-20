"""GraphQL fetch layer for the desired-state source (Phase 2 Step 1).

One pinned query, empirically checked against the live dev Nautobot instance
(2026-07-15): nodes, endpoints, IP ranges, operational configs (with their
local/tailscale endpoint relations), service placements, services, and
dependencies in a single round trip. As in `dnsmasq_query.py`, Nautobot's
GraphQL layer serializes ChoiceField values (`lifecycle`, `node_type`,
`endpoint_type`, `ip_policy`, ...) as their UPPERCASE enum *name*; every
choice field here is lowercased back to the vocabulary the ported nintent
logic (Steps 2 and 4) expects. Free-form JSON fields (`config`,
`dnsmasq_options`, `requirements`, `placement_policy`) round-trip untouched.

This is a superset of `dnsmasq_query.py`'s desired-side fetch (endpoints + IP
ranges); Step 4 switches `render dnsmasq` onto this module instead of
maintaining two desired-state queries.

Step 4 addition: `desired_nodes.accepted_actual_types`/`expected_spec` and
`desired_endpoints.realized_ip_address` are pinned here (empirically checked
against the live dev Nautobot instance, 2026-07-15) because the ported
`drift/evaluation.py` node/endpoint matching needs them — these are real
JSONField/ForeignKey fields on the nintent models, not derived, so adding them
here is a schema-completeness fix rather than new domain logic.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from nctl_core.nautobot import NautobotClient

DESIRED_QUERY = """
{
  desired_nodes {
    id
    slug
    name
    lifecycle
    node_type
    role
    accepted_actual_types
    expected_spec
    realized_device { id }
    realized_device_source
    realized_vm { id }
    realized_vm_source
  }
  desired_endpoints {
    id
    name
    endpoint_type
    ip_address
    ip_policy
    dns_name
    dns_name_source
    mdns_name
    mdns_name_source
    vpn_dns_name
    protocol
    port
    generate_dnsmasq
    dnsmasq_record_type
    realized_ip_address { id }
    realized_ip_address_source
    desired_node { id slug }
  }
  desired_ip_ranges {
    id
    name
    slug
    start_address
    end_address
    range_policy
    lifecycle
    generate_dnsmasq
    dnsmasq_options
  }
  desired_node_operational_overrides {
    id
    desired_node { id }
    declared_host_os
    connection_path
    ansible_port
    power_control
    is_laptop
    local_endpoint { id name endpoint_type ip_address dns_name mdns_name desired_node { slug } }
    tailscale_endpoint { id name endpoint_type ip_address dns_name mdns_name desired_node { slug } }
  }
  desired_service_placements {
    id
    desired_service { id }
    desired_node { id }
    desired_endpoint { id }
    instance_name
    desired_state
    instance_role
    deployment_profile
    config_schema_version
    config
    assignment_source
  }
  desired_services {
    id
    slug
    name
    display_name
    service_type
    lifecycle
    catalog_namespace
    catalog_metadata_name
    requirements
    placement_policy
  }
  desired_dependencies {
    id
    source_service { id }
    dependency_kind
    namespace
    name
    raw_ref
    dependency_type
    resolution_status
    resolved_service { id }
  }
}
"""


class DesiredEndpointRef(BaseModel):
    """A node-scoped endpoint as referenced from an operational override."""

    id: str
    name: str
    endpoint_type: str
    node_slug: str
    ip_address: str | None = None
    dns_name: str | None = None
    mdns_name: str | None = None


class DesiredNode(BaseModel):
    id: str
    slug: str
    name: str
    lifecycle: str
    node_type: str
    role: str | None = None
    accepted_actual_types: list[str] = []
    expected_spec: dict[str, Any] = {}
    realized_device_id: str | None = None
    realized_device_source: str | None = None
    realized_vm_id: str | None = None
    realized_vm_source: str | None = None


class DesiredEndpoint(BaseModel):
    id: str
    name: str
    endpoint_type: str
    node_id: str
    node_slug: str
    ip_address: str | None = None
    ip_policy: str = "static"
    dns_name: str | None = None
    dns_name_source: str | None = None
    mdns_name: str | None = None
    mdns_name_source: str | None = None
    vpn_dns_name: str | None = None
    protocol: str | None = None
    port: int | None = None
    generate_dnsmasq: bool = False
    dnsmasq_record_type: str = "host_record"
    realized_ip_address_id: str | None = None
    realized_ip_address_source: str | None = None


class DesiredIPRange(BaseModel):
    id: str
    name: str
    slug: str
    start_address: str
    end_address: str
    range_policy: str
    lifecycle: str
    generate_dnsmasq: bool = False
    dnsmasq_options: dict[str, Any] = {}


class DesiredNodeOperationalOverride(BaseModel):
    id: str
    node_id: str
    declared_host_os: str | None = None
    connection_path: str | None = None
    ansible_port: int | None = None
    power_control: str | None = None
    is_laptop: bool | None = None
    local_endpoint: DesiredEndpointRef | None = None
    tailscale_endpoint: DesiredEndpointRef | None = None


class DesiredServicePlacement(BaseModel):
    id: str
    service_id: str
    node_id: str
    endpoint_id: str | None = None
    instance_name: str
    desired_state: str = "active"
    instance_role: str | None = None
    deployment_profile: str
    config_schema_version: str
    config: dict[str, Any] = {}
    assignment_source: str = "manual"


class DesiredService(BaseModel):
    id: str
    slug: str
    name: str
    display_name: str
    service_type: str
    lifecycle: str
    catalog_namespace: str
    catalog_metadata_name: str
    requirements: dict[str, Any] = {}
    placement_policy: dict[str, Any] = {}


class DesiredDependency(BaseModel):
    id: str
    source_service_id: str
    dependency_kind: str
    namespace: str
    name: str
    raw_ref: str
    dependency_type: str
    resolution_status: str = "unresolved"
    resolved_service_id: str | None = None


class DesiredSnapshot(BaseModel):
    nodes: list[DesiredNode] = []
    endpoints: list[DesiredEndpoint] = []
    ip_ranges: list[DesiredIPRange] = []
    operational_overrides: list[DesiredNodeOperationalOverride] = []
    placements: list[DesiredServicePlacement] = []
    services: list[DesiredService] = []
    dependencies: list[DesiredDependency] = []


def fetch_desired_snapshot(client: NautobotClient) -> DesiredSnapshot:
    data = client.graphql(DESIRED_QUERY)
    return DesiredSnapshot(
        nodes=[_build_node(row) for row in data["desired_nodes"]],
        endpoints=[_build_endpoint(row) for row in data["desired_endpoints"]],
        ip_ranges=[_build_ip_range(row) for row in data["desired_ip_ranges"]],
        operational_overrides=[
            _build_operational_override(row) for row in data["desired_node_operational_overrides"]
        ],
        placements=[_build_placement(row) for row in data["desired_service_placements"]],
        services=[_build_service(row) for row in data["desired_services"]],
        dependencies=[_build_dependency(row) for row in data["desired_dependencies"]],
    )


def _build_node(row: dict[str, Any]) -> DesiredNode:
    realized_device = row.get("realized_device")
    realized_vm = row.get("realized_vm")
    return DesiredNode(
        id=row["id"],
        slug=row["slug"],
        name=row["name"],
        lifecycle=_lower(row["lifecycle"]),
        node_type=_lower(row["node_type"]),
        role=row.get("role"),
        accepted_actual_types=[_lower(item) for item in (row.get("accepted_actual_types") or [])],
        expected_spec=row.get("expected_spec") or {},
        realized_device_id=realized_device["id"] if realized_device else None,
        realized_device_source=_lower(row.get("realized_device_source")),
        realized_vm_id=realized_vm["id"] if realized_vm else None,
        realized_vm_source=_lower(row.get("realized_vm_source")),
    )


def _build_endpoint(row: dict[str, Any]) -> DesiredEndpoint:
    node = row["desired_node"]
    realized_ip_address = row.get("realized_ip_address")
    return DesiredEndpoint(
        id=row["id"],
        name=row["name"],
        endpoint_type=_lower(row["endpoint_type"]),
        node_id=node["id"],
        node_slug=node["slug"],
        ip_address=row.get("ip_address"),
        ip_policy=_lower(row.get("ip_policy")) or "static",
        dns_name=row.get("dns_name"),
        dns_name_source=_lower(row.get("dns_name_source")),
        mdns_name=row.get("mdns_name"),
        mdns_name_source=_lower(row.get("mdns_name_source")),
        vpn_dns_name=row.get("vpn_dns_name"),
        protocol=row.get("protocol"),
        port=row.get("port"),
        generate_dnsmasq=bool(row.get("generate_dnsmasq")),
        dnsmasq_record_type=_lower(row.get("dnsmasq_record_type")) or "host_record",
        realized_ip_address_id=realized_ip_address["id"] if realized_ip_address else None,
        realized_ip_address_source=_lower(row.get("realized_ip_address_source")),
    )


def _build_ip_range(row: dict[str, Any]) -> DesiredIPRange:
    return DesiredIPRange(
        id=row["id"],
        name=row["name"],
        slug=row["slug"],
        start_address=row["start_address"],
        end_address=row["end_address"],
        range_policy=_lower(row["range_policy"]),
        lifecycle=_lower(row["lifecycle"]),
        generate_dnsmasq=bool(row.get("generate_dnsmasq")),
        dnsmasq_options=row.get("dnsmasq_options") or {},
    )


def _build_endpoint_ref(row: dict[str, Any] | None) -> DesiredEndpointRef | None:
    if row is None:
        return None
    return DesiredEndpointRef(
        id=row["id"],
        name=row["name"],
        endpoint_type=_lower(row["endpoint_type"]),
        node_slug=row["desired_node"]["slug"],
        ip_address=row.get("ip_address"),
        dns_name=row.get("dns_name"),
        mdns_name=row.get("mdns_name"),
    )


def _build_operational_override(row: dict[str, Any]) -> DesiredNodeOperationalOverride:
    return DesiredNodeOperationalOverride(
        id=row["id"],
        node_id=row["desired_node"]["id"],
        declared_host_os=_lower(row.get("declared_host_os")),
        connection_path=_lower(row.get("connection_path")),
        ansible_port=row.get("ansible_port"),
        power_control=_lower(row.get("power_control")),
        is_laptop=row.get("is_laptop"),
        local_endpoint=_build_endpoint_ref(row.get("local_endpoint")),
        tailscale_endpoint=_build_endpoint_ref(row.get("tailscale_endpoint")),
    )


def _build_placement(row: dict[str, Any]) -> DesiredServicePlacement:
    endpoint = row.get("desired_endpoint")
    return DesiredServicePlacement(
        id=row["id"],
        service_id=row["desired_service"]["id"],
        node_id=row["desired_node"]["id"],
        endpoint_id=endpoint["id"] if endpoint else None,
        instance_name=row["instance_name"],
        desired_state=_lower(row.get("desired_state")) or "active",
        instance_role=row.get("instance_role"),
        deployment_profile=row["deployment_profile"],
        config_schema_version=row["config_schema_version"],
        config=row.get("config") or {},
        assignment_source=_lower(row.get("assignment_source")) or "manual",
    )


def _build_service(row: dict[str, Any]) -> DesiredService:
    return DesiredService(
        id=row["id"],
        slug=row["slug"],
        name=row["name"],
        display_name=row["display_name"],
        service_type=_lower(row["service_type"]),
        lifecycle=_lower(row["lifecycle"]),
        catalog_namespace=row["catalog_namespace"],
        catalog_metadata_name=row["catalog_metadata_name"],
        requirements=row.get("requirements") or {},
        placement_policy=row.get("placement_policy") or {},
    )


def _build_dependency(row: dict[str, Any]) -> DesiredDependency:
    resolved = row.get("resolved_service")
    return DesiredDependency(
        id=row["id"],
        source_service_id=row["source_service"]["id"],
        dependency_kind=row["dependency_kind"],
        namespace=row["namespace"],
        name=row["name"],
        raw_ref=row["raw_ref"],
        dependency_type=row["dependency_type"],
        resolution_status=_lower(row.get("resolution_status")) or "unresolved",
        resolved_service_id=resolved["id"] if resolved else None,
    )


def _lower(value: Any) -> Any:
    return value.lower() if isinstance(value, str) else value
