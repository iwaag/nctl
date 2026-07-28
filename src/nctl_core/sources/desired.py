"""GraphQL fetch layer for the desired-state source (Phase 2 Step 1).

One pinned query, empirically checked against the live dev Nautobot instance
(2026-07-15): nodes, endpoints, IP ranges, operational configs (with their
local/tailscale endpoint relations), service placements, services, and
dependencies in a single round trip. As in `dnsmasq_query.py`, Nautobot's
GraphQL layer serializes ChoiceField values (`lifecycle`, `node_type`,
`endpoint_type`, `ip_policy`, ...) as their UPPERCASE enum *name*; every
choice field here is lowercased back to the vocabulary the ported nintent
logic (Steps 2 and 4) expects. Free-form JSON fields (`config`,
`dnsmasq_options`, `requirements`) round-trip untouched. `placement_policy` was removed from
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

VM Phase 3 Step 5 addition (destructive, coordinated with the matched nintent
revision, no dual-read): `DesiredNode.realized_vm(+_source)` is REMOVED
outright (legacy field, superseded by
`DesiredComputeInstance.realized_vm(+_source)`), `DesiredEndpoint.mac_address`
is added, and two new roots are fetched -- `desired_compute_platforms` and
`desired_compute_instances` -- mirroring `nintent/nautobot_intent_catalog/
models.py`'s `DesiredComputePlatform`/`DesiredComputeInstance` and the shared
pure contracts in `nintent/nautobot_intent_catalog/compute_contract.py`
(`normalize_mac_address`, `effective_lifecycle`, `effective_value`, the
platform/instance config validators). nctl does not import that nintent
module (separate deployable, no shared runtime dependency); the logic is
ported here deliberately kept behaviorally identical.

Compute rows are read/typed/validated only -- no compute drift, planner, or
reconcile action is added in this step (Phase 4/5 territory per plan.md
Section 5.1). A malformed compute platform/instance/endpoint-MAC row
never raises out of `fetch_desired_snapshot()`; it is converted into a
`DesiredSourceIssue` and excluded from the typed `compute_platforms`/
`compute_instances` collections, while the rest of the snapshot (including
unrelated healthy nodes/endpoints) parses normally in the same call. Only a
row whose own identity is unknowable (missing `id`) propagates as a normal
`NautobotError`/`NautobotGraphQLError`, same as any other root in this query.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

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
    _validate_link_source_xnor,
    effective_compute_defaults,
    effective_lifecycle,
    effective_single_source_value,
    effective_value,
    endpoint_has_usable_ip,
    endpoint_satisfies_compute_address_contract,
    is_actionable_lifecycle,
    normalize_mac_address,
    select_compute_primary_endpoint,
    validate_compute_lifecycle,
    validate_config_schema_version,
    validate_instance_config,
    validate_instance_kind,
    validate_memory_mb,
    validate_platform_config,
    validate_power_state,
    validate_provider_type,
    validate_root_disk_gb,
    validate_vcpus,
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
    realized_device_source
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
    mac_address
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
  desired_compute_platforms {
    id
    name
    slug
    provider_type
    lifecycle
    control_node { id slug }
    config_schema_version
    config
    realized_cluster { id }
    realized_cluster_source
  }
  desired_compute_instances {
    id
    desired_node { id slug }
    platform { id slug }
    instance_kind
    desired_power_state
    vcpus
    memory_mb
    root_disk_gb
    config_schema_version
    config
    realized_vm { id }
    realized_vm_source
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
    mac_address: str | None = None
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
    compute_platforms: list[DesiredComputePlatform] = []
    compute_instances: list[DesiredComputeInstance] = []
    source_issues: list[DesiredSourceIssue] = []


def fetch_desired_snapshot(client: NautobotClient) -> DesiredSnapshot:
    data = client.graphql(DESIRED_QUERY)
    endpoints = [_build_endpoint(row) for row in data["desired_endpoints"]]
    nodes = [_build_node(row) for row in data["desired_nodes"]]
    compute_platforms, compute_instances, source_issues = _build_compute_collections(
        data.get("desired_compute_platforms") or [],
        data.get("desired_compute_instances") or [],
        nodes=nodes,
        endpoints=endpoints,
    )
    source_issues.extend(_validate_endpoint_macs(data["desired_endpoints"], endpoints))
    return DesiredSnapshot(
        nodes=nodes,
        endpoints=endpoints,
        ip_ranges=[_build_ip_range(row) for row in data["desired_ip_ranges"]],
        operational_overrides=[
            _build_operational_override(row) for row in data["desired_node_operational_overrides"]
        ],
        placements=[_build_placement(row) for row in data["desired_service_placements"]],
        services=[_build_service(row) for row in data["desired_services"]],
        dependencies=[_build_dependency(row) for row in data["desired_dependencies"]],
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
        realized_device_source=_lower(row.get("realized_device_source")),
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
        mac_address=_canonical_mac_or_none(row.get("mac_address")),
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


def _canonical_mac_or_none(value: Any) -> str | None:
    """Normalize a decoded MAC while preserving decoder tolerance."""
    try:
        return normalize_mac_address(value)
    except ComputeContractError:
        return None


def _build_compute_platform(row: dict[str, Any]) -> DesiredComputePlatform:
    control_node = row.get("control_node")
    if not control_node:
        raise ComputeContractError("missing_control_node", "control_node is required", path="control_node")
    realized_cluster = row.get("realized_cluster")
    realized_cluster_source = _validate_link_source_xnor(
        realized_cluster, row.get("realized_cluster_source"), path="realized_cluster_source"
    )
    return DesiredComputePlatform(
        id=row["id"],
        name=row["name"],
        slug=row["slug"],
        provider_type=validate_provider_type(_lower(row.get("provider_type"))),
        lifecycle=validate_compute_lifecycle(_lower(row.get("lifecycle"))),
        control_node_id=control_node["id"],
        config_schema_version=validate_config_schema_version(row.get("config_schema_version")),
        config=validate_platform_config(row.get("config")),
        realized_cluster_id=realized_cluster["id"] if realized_cluster else None,
        realized_cluster_source=realized_cluster_source,
    )


def _build_compute_instance(row: dict[str, Any]) -> DesiredComputeInstance:
    desired_node = row.get("desired_node")
    if not desired_node:
        raise ComputeContractError("missing_desired_node", "desired_node is required", path="desired_node")
    platform = row.get("platform")
    if not platform:
        raise ComputeContractError("missing_platform", "platform is required", path="platform")
    instance_kind = validate_instance_kind(_lower(row.get("instance_kind")))
    realized_vm = row.get("realized_vm")
    realized_vm_source = _validate_link_source_xnor(
        realized_vm, row.get("realized_vm_source"), path="realized_vm_source"
    )
    return DesiredComputeInstance(
        id=row["id"],
        desired_node_id=desired_node["id"],
        platform_id=platform["id"],
        instance_kind=instance_kind,
        desired_power_state=validate_power_state(_lower(row.get("desired_power_state")) or "running"),
        vcpus=validate_vcpus(row.get("vcpus")),
        memory_mb=validate_memory_mb(row.get("memory_mb")),
        root_disk_gb=validate_root_disk_gb(row.get("root_disk_gb")),
        config_schema_version=validate_config_schema_version(row.get("config_schema_version")),
        config=validate_instance_config(row.get("config"), instance_kind=instance_kind),
        realized_vm_id=realized_vm["id"] if realized_vm else None,
        realized_vm_source=realized_vm_source,
    )


def _build_compute_collections(
    platform_rows: list[dict[str, Any]],
    instance_rows: list[dict[str, Any]],
    *,
    nodes: list[DesiredNode],
    endpoints: list[DesiredEndpoint],
) -> tuple[list[DesiredComputePlatform], list[DesiredComputeInstance], list[DesiredSourceIssue]]:
    """Row-scoped compute validation (plan.md Section 5.9).

    No invalid row is silently discarded and no invalid row is allowed to
    crash the rest of the snapshot: every row is parsed independently, a bad
    row becomes a `DesiredSourceIssue`, and only rows that pass every check
    (including, for an active/approved effective instance, the endpoint
    topology-completeness check) end up in the returned typed collections.
    An invalid platform's issue lists every instance it dependency-blocks in
    `blocked_consumers`.
    """
    nodes_by_id = {node.id: node for node in nodes}
    endpoints_by_node_id: dict[str, list[DesiredEndpoint]] = {}
    for endpoint in endpoints:
        endpoints_by_node_id.setdefault(endpoint.node_id, []).append(endpoint)

    issues: list[DesiredSourceIssue] = []

    # --- Parse platform rows independently; a bad row never blocks another. ---
    raw_platforms: list[DesiredComputePlatform] = []
    for row in platform_rows:
        row_id = row["id"]  # identity-unknowable row (no id) legitimately propagates
        try:
            raw_platforms.append(_build_compute_platform(row))
        except (KeyError, TypeError, ValueError) as exc:
            issues.append(
                DesiredSourceIssue(
                    code=getattr(exc, "code", "invalid_compute_platform"),
                    target_kind="compute_platform",
                    target_id=row_id,
                    target_slug_or_name=_text_or_none(row.get("slug") or row.get("name")),
                    scope="target",
                    message=str(exc),
                    evidence={"row_id": row_id},
                )
            )

    # --- Unique platform slug: a collision is ambiguous, so exclude every colliding row. ---
    platforms_by_slug: dict[str, list[DesiredComputePlatform]] = {}
    for platform in raw_platforms:
        platforms_by_slug.setdefault(platform.slug, []).append(platform)

    valid_platforms: dict[str, DesiredComputePlatform] = {}
    invalid_platform_ids: set[str] = set()
    for slug, group in platforms_by_slug.items():
        if len(group) > 1:
            colliding_ids = [platform.id for platform in group]
            for platform in group:
                invalid_platform_ids.add(platform.id)
                issues.append(
                    DesiredSourceIssue(
                        code="duplicate_platform_slug",
                        target_kind="compute_platform",
                        target_id=platform.id,
                        target_slug_or_name=platform.slug,
                        scope="global",
                        message=f"slug {slug!r} is used by more than one DesiredComputePlatform",
                        evidence={"colliding_platform_ids": colliding_ids},
                    )
                )
            continue
        valid_platforms[group[0].id] = group[0]

    # --- Referenced control_node must exist in this snapshot. ---
    for platform_id in list(valid_platforms):
        platform = valid_platforms[platform_id]
        if platform.control_node_id not in nodes_by_id:
            invalid_platform_ids.add(platform.id)
            del valid_platforms[platform_id]
            issues.append(
                DesiredSourceIssue(
                    code="compute_platform_control_node_missing",
                    target_kind="compute_platform",
                    target_id=platform.id,
                    target_slug_or_name=platform.slug,
                    scope="target",
                    message=f"control_node {platform.control_node_id!r} does not exist in this snapshot",
                    evidence={"control_node_id": platform.control_node_id},
                )
            )

    blocked_consumers_by_platform_id: dict[str, list[str]] = {
        platform_id: [] for platform_id in invalid_platform_ids
    }

    # --- Parse instance rows independently. ---
    raw_instances: list[DesiredComputeInstance] = []
    for row in instance_rows:
        row_id = row["id"]
        try:
            raw_instances.append(_build_compute_instance(row))
        except (KeyError, TypeError, ValueError) as exc:
            issues.append(
                DesiredSourceIssue(
                    code=getattr(exc, "code", "invalid_compute_instance"),
                    target_kind="compute_instance",
                    target_id=row_id,
                    target_slug_or_name=None,
                    scope="target",
                    message=str(exc),
                    evidence={"row_id": row_id},
                )
            )

    # --- One instance per node: keep the first, flag the rest. ---
    instances_by_node: dict[str, list[DesiredComputeInstance]] = {}
    for instance in raw_instances:
        instances_by_node.setdefault(instance.desired_node_id, []).append(instance)

    deduped_instances: list[DesiredComputeInstance] = []
    for node_id, group in instances_by_node.items():
        deduped_instances.append(group[0])
        if len(group) > 1:
            for duplicate in group[1:]:
                issues.append(
                    DesiredSourceIssue(
                        code="duplicate_compute_instance_for_node",
                        target_kind="compute_instance",
                        target_id=duplicate.id,
                        target_slug_or_name=None,
                        scope="target",
                        message=f"desired_node {node_id!r} already has a DesiredComputeInstance",
                        evidence={"desired_node_id": node_id, "kept_instance_id": group[0].id},
                    )
                )

    # --- References must exist; a reference to a known-invalid platform is dependency-blocked. ---
    final_instances: list[DesiredComputeInstance] = []
    for instance in deduped_instances:
        if instance.desired_node_id not in nodes_by_id:
            issues.append(
                DesiredSourceIssue(
                    code="compute_instance_node_missing",
                    target_kind="compute_instance",
                    target_id=instance.id,
                    target_slug_or_name=None,
                    scope="target",
                    message=f"desired_node {instance.desired_node_id!r} does not exist in this snapshot",
                    evidence={"desired_node_id": instance.desired_node_id},
                )
            )
            continue
        if instance.platform_id in invalid_platform_ids:
            blocked_consumers_by_platform_id.setdefault(instance.platform_id, []).append(instance.id)
            issues.append(
                DesiredSourceIssue(
                    code="compute_instance_platform_invalid",
                    target_kind="compute_instance",
                    target_id=instance.id,
                    target_slug_or_name=None,
                    scope="target",
                    message=f"platform {instance.platform_id!r} failed its own validation",
                    evidence={"platform_id": instance.platform_id},
                )
            )
            continue
        if instance.platform_id not in valid_platforms:
            issues.append(
                DesiredSourceIssue(
                    code="compute_instance_platform_missing",
                    target_kind="compute_instance",
                    target_id=instance.id,
                    target_slug_or_name=None,
                    scope="target",
                    message=f"platform {instance.platform_id!r} does not exist in this snapshot",
                    evidence={"platform_id": instance.platform_id},
                )
            )
            continue
        final_instances.append(instance)

    # --- Effective active/approved topology completeness. ---
    ready_instances: list[DesiredComputeInstance] = []
    for instance in final_instances:
        node = nodes_by_id[instance.desired_node_id]
        platform = valid_platforms[instance.platform_id]
        effective = effective_lifecycle(node.lifecycle, platform.lifecycle)
        if is_actionable_lifecycle(effective):
            _selected, code = select_compute_primary_endpoint(endpoints_by_node_id.get(node.id, []))
            if code is not None:
                if code == COMPUTE_PRIMARY_ENDPOINT_MISSING:
                    message = f"node {node.slug!r} has no primary endpoint satisfying the compute NIC contract"
                else:
                    message = (
                        f"node {node.slug!r} has more than one primary endpoint satisfying the compute NIC contract"
                    )
                issues.append(
                    DesiredSourceIssue(
                        code=code,
                        target_kind="compute_instance",
                        target_id=instance.id,
                        target_slug_or_name=None,
                        scope="target",
                        message=message,
                        evidence={"desired_node_id": node.id, "effective_lifecycle": effective},
                    )
                )
                continue
        ready_instances.append(instance)

    # --- Attach blocked_consumers onto the platform issue(s) they dependency-block. ---
    if blocked_consumers_by_platform_id:
        for issue in issues:
            if issue.target_kind != "compute_platform" or issue.target_id not in blocked_consumers_by_platform_id:
                continue
            consumers = blocked_consumers_by_platform_id[issue.target_id]
            if consumers:
                issue.blocked_consumers = sorted(set(issue.blocked_consumers) | set(consumers))

    return list(valid_platforms.values()), ready_instances, issues


def _validate_endpoint_macs(
    rows: list[dict[str, Any]], endpoints: list[DesiredEndpoint]
) -> list[DesiredSourceIssue]:
    """Malformed/duplicate desired MACs, scoped to the owning endpoint (plan.md Section 5.5/5.9).

    `rows`/`endpoints` are the same GraphQL rows and `_build_endpoint`
    outputs, in the same order -- `_build_endpoint` never raises on a bad
    MAC (it stores `None`), so the raw row is re-checked here to distinguish
    "malformed" from "legitimately absent" and to produce the diagnostic.
    """
    issues: list[DesiredSourceIssue] = []
    endpoint_id_by_mac: dict[str, str] = {}
    for row, endpoint in zip(rows, endpoints):
        raw_mac = row.get("mac_address")
        try:
            normalize_mac_address(raw_mac)
        except ComputeContractError as exc:
            issues.append(
                DesiredSourceIssue(
                    code="invalid_mac_address",
                    target_kind="endpoint",
                    target_id=endpoint.id,
                    target_slug_or_name=endpoint.name,
                    scope="target",
                    message=str(exc),
                    evidence={"raw_mac_address": raw_mac},
                )
            )
            continue
        if endpoint.mac_address is None:
            continue
        existing_endpoint_id = endpoint_id_by_mac.get(endpoint.mac_address)
        if existing_endpoint_id is not None:
            issues.append(
                DesiredSourceIssue(
                    code="duplicate_mac_address",
                    target_kind="endpoint",
                    target_id=endpoint.id,
                    target_slug_or_name=endpoint.name,
                    scope="global",
                    message=f"MAC address {endpoint.mac_address!r} is already used by another endpoint",
                    evidence={"mac_address": endpoint.mac_address, "conflicting_endpoint_id": existing_endpoint_id},
                )
            )
        else:
            endpoint_id_by_mac[endpoint.mac_address] = endpoint.id
    return issues


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _lower(value: Any) -> Any:
    return value.lower() if isinstance(value, str) else value
