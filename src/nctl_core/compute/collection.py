"""Pure compute collection assembly and source-issue policy."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .contract import (
    COMPUTE_PRIMARY_ENDPOINT_MISSING,
    ComputeContractError,
    effective_lifecycle,
    is_actionable_lifecycle,
    normalize_mac_address,
    select_compute_primary_endpoint,
    validate_compute_lifecycle,
    validate_instance_config,
    validate_instance_kind,
    validate_memory_mb,
    validate_platform_config,
    validate_desired_presence,
    validate_power_state,
    validate_root_disk_gb,
    validate_vcpus,
)
from .model import DesiredComputeInstance, DesiredComputePlatform, DesiredSourceIssue

if TYPE_CHECKING:
    from nctl_core.sources.desired import DesiredEndpoint, DesiredNode


def _lower(value: Any) -> Any:
    return value.lower() if isinstance(value, str) else value


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _build_compute_platform(row: dict[str, Any]) -> DesiredComputePlatform:
    control_node = row.get("control_node")
    if not control_node:
        raise ComputeContractError("missing_control_node", "control_node is required", path="control_node")
    realized_cluster = row.get("realized_cluster")
    return DesiredComputePlatform(
        id=row["id"], name=row["name"], slug=row["slug"],
        lifecycle=validate_compute_lifecycle(_lower(row.get("lifecycle"))),
        control_node_id=control_node["id"],
        config=validate_platform_config(row.get("config")),
        realized_cluster_id=realized_cluster["id"] if realized_cluster else None,
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
    return DesiredComputeInstance(
        id=row["id"], desired_node_id=desired_node["id"], platform_id=platform["id"], instance_kind=instance_kind,
        desired_power_state=validate_power_state(_lower(row.get("desired_power_state")) or "running"),
        desired_presence=validate_desired_presence(_lower(row.get("desired_presence")) or "present"),
        vcpus=validate_vcpus(row.get("vcpus")), memory_mb=validate_memory_mb(row.get("memory_mb")),
        root_disk_gb=validate_root_disk_gb(row.get("root_disk_gb")),
        config=validate_instance_config(row.get("config"), instance_kind=instance_kind),
        realized_vm_id=realized_vm["id"] if realized_vm else None,
    )


def build_compute_collections(
    platform_rows: list[dict[str, Any]], instance_rows: list[dict[str, Any]], *,
    nodes: list[DesiredNode], endpoints: list[DesiredEndpoint],
) -> tuple[list[DesiredComputePlatform], list[DesiredComputeInstance], list[DesiredSourceIssue]]:
    """Build valid compute rows and retain every row-scoped validation issue."""
    nodes_by_id = {node.id: node for node in nodes}
    endpoints_by_node_id: dict[str, list[DesiredEndpoint]] = {}
    for endpoint in endpoints:
        endpoints_by_node_id.setdefault(endpoint.node_id, []).append(endpoint)
    issues: list[DesiredSourceIssue] = []
    raw_platforms: list[DesiredComputePlatform] = []
    for row in platform_rows:
        row_id = row["id"]
        try:
            raw_platforms.append(_build_compute_platform(row))
        except (KeyError, TypeError, ValueError) as exc:
            issues.append(DesiredSourceIssue(code=getattr(exc, "code", "invalid_compute_platform"), target_kind="compute_platform", target_id=row_id, target_slug_or_name=_text_or_none(row.get("slug") or row.get("name")), scope="target", message=str(exc), evidence={"row_id": row_id}))
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
                issues.append(DesiredSourceIssue(code="duplicate_platform_slug", target_kind="compute_platform", target_id=platform.id, target_slug_or_name=platform.slug, scope="global", message=f"slug {slug!r} is used by more than one DesiredComputePlatform", evidence={"colliding_platform_ids": colliding_ids}))
            continue
        valid_platforms[group[0].id] = group[0]
    for platform_id in list(valid_platforms):
        platform = valid_platforms[platform_id]
        if platform.control_node_id not in nodes_by_id:
            invalid_platform_ids.add(platform.id)
            del valid_platforms[platform_id]
            issues.append(DesiredSourceIssue(code="compute_platform_control_node_missing", target_kind="compute_platform", target_id=platform.id, target_slug_or_name=platform.slug, scope="target", message=f"control_node {platform.control_node_id!r} does not exist in this snapshot", evidence={"control_node_id": platform.control_node_id}))
    blocked_consumers_by_platform_id: dict[str, list[str]] = {platform_id: [] for platform_id in invalid_platform_ids}
    raw_instances: list[DesiredComputeInstance] = []
    for row in instance_rows:
        row_id = row["id"]
        try:
            raw_instances.append(_build_compute_instance(row))
        except (KeyError, TypeError, ValueError) as exc:
            issues.append(DesiredSourceIssue(code=getattr(exc, "code", "invalid_compute_instance"), target_kind="compute_instance", target_id=row_id, target_slug_or_name=None, scope="target", message=str(exc), evidence={"row_id": row_id}))
    instances_by_node: dict[str, list[DesiredComputeInstance]] = {}
    for instance in raw_instances:
        instances_by_node.setdefault(instance.desired_node_id, []).append(instance)
    deduped_instances: list[DesiredComputeInstance] = []
    for node_id, group in instances_by_node.items():
        deduped_instances.append(group[0])
        if len(group) > 1:
            for duplicate in group[1:]:
                issues.append(DesiredSourceIssue(code="duplicate_compute_instance_for_node", target_kind="compute_instance", target_id=duplicate.id, target_slug_or_name=None, scope="target", message=f"desired_node {node_id!r} already has a DesiredComputeInstance", evidence={"desired_node_id": node_id, "kept_instance_id": group[0].id}))
    final_instances: list[DesiredComputeInstance] = []
    for instance in deduped_instances:
        if instance.desired_node_id not in nodes_by_id:
            issues.append(DesiredSourceIssue(code="compute_instance_node_missing", target_kind="compute_instance", target_id=instance.id, target_slug_or_name=None, scope="target", message=f"desired_node {instance.desired_node_id!r} does not exist in this snapshot", evidence={"desired_node_id": instance.desired_node_id}))
            continue
        if instance.platform_id in invalid_platform_ids:
            blocked_consumers_by_platform_id.setdefault(instance.platform_id, []).append(instance.id)
            issues.append(DesiredSourceIssue(code="compute_instance_platform_invalid", target_kind="compute_instance", target_id=instance.id, target_slug_or_name=None, scope="target", message=f"platform {instance.platform_id!r} failed its own validation", evidence={"platform_id": instance.platform_id}))
            continue
        if instance.platform_id not in valid_platforms:
            issues.append(DesiredSourceIssue(code="compute_instance_platform_missing", target_kind="compute_instance", target_id=instance.id, target_slug_or_name=None, scope="target", message=f"platform {instance.platform_id!r} does not exist in this snapshot", evidence={"platform_id": instance.platform_id}))
            continue
        final_instances.append(instance)
    ready_instances: list[DesiredComputeInstance] = []
    for instance in final_instances:
        node = nodes_by_id[instance.desired_node_id]
        platform = valid_platforms[instance.platform_id]
        effective = effective_lifecycle(node.lifecycle, platform.lifecycle)
        if is_actionable_lifecycle(effective):
            _selected, code = select_compute_primary_endpoint(endpoints_by_node_id.get(node.id, []))
            if code is not None:
                message = f"node {node.slug!r} has no primary endpoint satisfying the compute NIC contract" if code == COMPUTE_PRIMARY_ENDPOINT_MISSING else f"node {node.slug!r} has more than one primary endpoint satisfying the compute NIC contract"
                issues.append(DesiredSourceIssue(code=code, target_kind="compute_instance", target_id=instance.id, target_slug_or_name=None, scope="target", message=message, evidence={"desired_node_id": node.id, "effective_lifecycle": effective}))
                continue
        ready_instances.append(instance)
    for issue in issues:
        if issue.target_kind == "compute_platform" and issue.target_id in blocked_consumers_by_platform_id:
            consumers = blocked_consumers_by_platform_id[issue.target_id]
            if consumers:
                issue.blocked_consumers = sorted(set(issue.blocked_consumers) | set(consumers))
    return list(valid_platforms.values()), ready_instances, issues


def validate_endpoint_macs(rows: list[dict[str, Any]], endpoints: list[DesiredEndpoint]) -> list[DesiredSourceIssue]:
    """Return malformed and duplicate desired-MAC source issues."""
    issues: list[DesiredSourceIssue] = []
    endpoint_id_by_mac: dict[str, str] = {}
    for row, endpoint in zip(rows, endpoints):
        raw_mac = row.get("mac_address")
        try:
            normalize_mac_address(raw_mac)
        except ComputeContractError as exc:
            issues.append(DesiredSourceIssue(code="invalid_mac_address", target_kind="endpoint", target_id=endpoint.id, target_slug_or_name=endpoint.name, scope="target", message=str(exc), evidence={"raw_mac_address": raw_mac}))
            continue
        if endpoint.mac_address is None:
            continue
        existing_endpoint_id = endpoint_id_by_mac.get(endpoint.mac_address)
        if existing_endpoint_id is not None:
            issues.append(DesiredSourceIssue(code="duplicate_mac_address", target_kind="endpoint", target_id=endpoint.id, target_slug_or_name=endpoint.name, scope="global", message=f"MAC address {endpoint.mac_address!r} is already used by another endpoint", evidence={"mac_address": endpoint.mac_address, "conflicting_endpoint_id": existing_endpoint_id}))
        else:
            endpoint_id_by_mac[endpoint.mac_address] = endpoint.id
    return issues
