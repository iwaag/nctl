"""GraphQL fetch layer for the desired-state source (Phase 2 Step 1).

One pinned query, empirically checked against the live dev Nautobot instance
(2026-07-15): nodes, endpoints, IP ranges, operational configs (with their
local/tailscale endpoint relations), service placements, services, and
dependencies in a single round trip. As in `dnsmasq_query.py`, Nautobot's
GraphQL layer serializes ChoiceField values (`lifecycle`, `node_type`,
`endpoint_type`, `ip_policy`, ...) as their UPPERCASE enum *name*; every
choice field here is lowercased back to the vocabulary the ported nintent
logic (Steps 2 and 4) expects. Free-form JSON fields (`config`, `dnsmasq_options`)
round-trip untouched. `placement_policy` was removed from
`DesiredService` in Phase 4 (better_usability p4) Decision 6 -- inert, no producer of non-empty
data, and no consumer; it is no longer fetched here or exposed on any typed model.

This is a superset of `dnsmasq_query.py`'s desired-side fetch (endpoints + IP
ranges); Step 4 switches `render dnsmasq` onto this module instead of
maintaining two desired-state queries.

Step 4 addition: `desired_nodes.accepted_actual_types`/`expected_spec` and
`desired_endpoints.realized_ip_address` are pinned here (empirically checked
against the live dev Nautobot instance, 2026-07-15) because the ported
`drift/evaluation.py` node/endpoint matching needs them — these are real
JSONField/ForeignKey fields on the nintent models, not derived, so adding them
here is a schema-completeness fix rather than new domain logic.

service_relation Phase 2 addition: `desired_service_bindings` (empirically
checked against the live scratch Nautobot, 2026-08-01) carries the
`DesiredServiceBinding` rows that replaced the old placement-config provider
key; the resolver in `production/service_dependencies.py` is their consumer.

creative_workspace p1 Step 2 addition: `desired_workspaces` (field names and
plural confirmed live in `p0/report_step6.md`) carries `DesiredWorkspace`
rows; `observation.py`'s `render_probe_hints` is the first consumer, driving
`workspace_probe_hints`. Decoded with `.get(...) or []` like the compute
roots below, not a required key, so existing fixtures that predate this
field keep working.

Compute roots are decoded here as transport data. Their pure row models,
fixture-bound validation, collection assembly, and source-issue policy live
in `nctl_core.compute`. Decode-time malformed-MAC tolerance stays here so an
endpoint row can always be decoded; the compute collection records any
corresponding source issue. Compute remains read-only and inert: no compute
drift, planner, reconciler, or actuator is owned by this transport module.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from nctl_core.compute.collection import build_compute_collections, validate_endpoint_macs
from nctl_core.compute.contract import (
    COMPUTE_LIFECYCLE_CHOICES,
    COMPUTE_PRIMARY_ENDPOINT_AMBIGUOUS,
    COMPUTE_PRIMARY_ENDPOINT_MISSING,
    CONFIG_SCHEMA_VERSION_V1,
    INSTANCE_KIND_CHOICES,
    MEMORY_MB_MAX,
    MEMORY_MB_MIN,
    POWER_STATE_CHOICES,
    PROVIDER_TYPE_CHOICES,
    PROVENANCE_INSTANCE_OVERRIDE,
    PROVENANCE_INTENT,
    PROVENANCE_PLATFORM_DEFAULT,
    PROVENANCE_UNRESOLVED,
    ROOT_DISK_GB_MAX,
    ROOT_DISK_GB_MIN,
    SOURCE_CHOICES,
    VCPUS_MAX,
    VCPUS_MIN,
    VMID_MAX,
    VMID_MIN,
    ComputeContractError,
    normalize_mac_address,
)
from nctl_core.compute.model import DesiredComputeInstance, DesiredComputePlatform, DesiredSourceIssue

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
  }
  desired_endpoints {
    id
    name
    endpoint_type
    ip_address
    gateway_address
    ip_policy
    dns_name
    mdns_name
    vpn_dns_name
    mac_address
    protocol
    port
    generate_dnsmasq
    dnsmasq_record_type
    realized_ip_address { id }
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
    deployment_profile
    config_schema_version
    config
  }
  desired_services {
    id
    slug
    name
    lifecycle
  }
  desired_service_bindings {
    id
    binding_name
    consumer_placement { id }
    provider_service { id slug }
  }
  desired_compute_platforms {
    id
    name
    slug
    lifecycle
    control_node { id slug }
    config
    realized_cluster { id }
  }
  desired_workspaces {
    id
    slug
    name
    lifecycle
    source_remote_url
    expected_path
    desired_presence
    desired_node { id slug }
  }
  desired_compute_instances {
    id
    desired_node { id slug }
    platform { id slug }
    instance_kind
    desired_power_state
    desired_presence
    vcpus
    memory_mb
    root_disk_gb
    config
    realized_vm { id }
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
    gateway_address: str | None = None
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


class DesiredEndpoint(BaseModel):
    id: str
    name: str
    endpoint_type: str
    node_id: str
    node_slug: str
    ip_address: str | None = None
    gateway_address: str | None = None
    ip_policy: str = "static"
    dns_name: str | None = None
    mdns_name: str | None = None
    vpn_dns_name: str | None = None
    mac_address: str | None = None
    protocol: str | None = None
    port: int | None = None
    generate_dnsmasq: bool = False
    dnsmasq_record_type: str = "host_record"
    realized_ip_address_id: str | None = None


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
    deployment_profile: str
    config_schema_version: str
    config: dict[str, Any] = {}


class DesiredService(BaseModel):
    id: str
    slug: str
    name: str
    lifecycle: str


class DesiredServiceBinding(BaseModel):
    """One service-to-service binding row (idea-A §3.1); identity is
    `(consumer_placement, binding_name)`, enforced by nintent at write time."""

    id: str
    binding_name: str
    consumer_placement_id: str
    provider_service_id: str
    provider_service_slug: str


class DesiredWorkspace(BaseModel):
    """A composite Git checkout under active development (creative_workspace p1 Step 2)."""

    id: str
    slug: str
    name: str
    lifecycle: str
    source_remote_url: str
    expected_path: str
    desired_presence: str
    node_id: str
    node_slug: str


class DesiredSnapshot(BaseModel):
    nodes: list[DesiredNode] = []
    endpoints: list[DesiredEndpoint] = []
    ip_ranges: list[DesiredIPRange] = []
    operational_overrides: list[DesiredNodeOperationalOverride] = []
    placements: list[DesiredServicePlacement] = []
    services: list[DesiredService] = []
    service_bindings: list[DesiredServiceBinding] = []
    workspaces: list[DesiredWorkspace] = []
    compute_platforms: list[DesiredComputePlatform] = []
    compute_instances: list[DesiredComputeInstance] = []
    source_issues: list[DesiredSourceIssue] = []


def fetch_desired_snapshot(client: NautobotClient) -> DesiredSnapshot:
    data = client.graphql(DESIRED_QUERY)
    endpoints = [_build_endpoint(row) for row in data["desired_endpoints"]]
    nodes = [_build_node(row) for row in data["desired_nodes"]]
    compute_platforms, compute_instances, source_issues = build_compute_collections(
        data.get("desired_compute_platforms") or [],
        data.get("desired_compute_instances") or [],
        nodes=nodes,
        endpoints=endpoints,
    )
    source_issues.extend(validate_endpoint_macs(data["desired_endpoints"], endpoints))
    return DesiredSnapshot(
        nodes=nodes,
        endpoints=endpoints,
        ip_ranges=[_build_ip_range(row) for row in data["desired_ip_ranges"]],
        operational_overrides=[
            _build_operational_override(row) for row in data["desired_node_operational_overrides"]
        ],
        placements=[_build_placement(row) for row in data["desired_service_placements"]],
        services=[_build_service(row) for row in data["desired_services"]],
        service_bindings=[_build_service_binding(row) for row in data["desired_service_bindings"]],
        workspaces=[_build_workspace(row) for row in data.get("desired_workspaces") or []],
        compute_platforms=compute_platforms,
        compute_instances=compute_instances,
        source_issues=source_issues,
    )


def _build_node(row: dict[str, Any]) -> DesiredNode:
    realized_device = row.get("realized_device")
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
        gateway_address=row.get("gateway_address"),
        ip_policy=_lower(row.get("ip_policy")) or "static",
        dns_name=row.get("dns_name"),
        mdns_name=row.get("mdns_name"),
        vpn_dns_name=row.get("vpn_dns_name"),
        mac_address=_canonical_mac_or_none(row.get("mac_address")),
        protocol=row.get("protocol"),
        port=row.get("port"),
        generate_dnsmasq=bool(row.get("generate_dnsmasq")),
        dnsmasq_record_type=_lower(row.get("dnsmasq_record_type")) or "host_record",
        realized_ip_address_id=realized_ip_address["id"] if realized_ip_address else None,
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
        deployment_profile=row["deployment_profile"],
        config_schema_version=row["config_schema_version"],
        config=row.get("config") or {},
    )


def _build_service(row: dict[str, Any]) -> DesiredService:
    return DesiredService(
        id=row["id"],
        slug=row["slug"],
        name=row["name"],
        lifecycle=_lower(row["lifecycle"]),
    )


def _build_workspace(row: dict[str, Any]) -> DesiredWorkspace:
    node = row["desired_node"]
    return DesiredWorkspace(
        id=row["id"],
        slug=row["slug"],
        name=row["name"],
        lifecycle=_lower(row["lifecycle"]),
        source_remote_url=row["source_remote_url"],
        expected_path=row["expected_path"],
        desired_presence=_lower(row["desired_presence"]),
        node_id=node["id"],
        node_slug=node["slug"],
    )


def _build_service_binding(row: dict[str, Any]) -> DesiredServiceBinding:
    provider = row["provider_service"]
    return DesiredServiceBinding(
        id=row["id"],
        binding_name=row["binding_name"],
        consumer_placement_id=row["consumer_placement"]["id"],
        provider_service_id=provider["id"],
        provider_service_slug=provider["slug"],
    )


def _canonical_mac_or_none(value: Any) -> str | None:
    """Normalize a decoded MAC while preserving decoder tolerance."""
    try:
        return normalize_mac_address(value)
    except ComputeContractError:
        return None


def _lower(value: Any) -> Any:
    return value.lower() if isinstance(value, str) else value
