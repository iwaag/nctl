"""`nctl desired export`: the complete current desired state as one canonical batch document.

Reads the same pinned GraphQL snapshot every other desired consumer uses
(`sources/desired.py`) and re-emits it as the Phase 0 batch envelope that
`nctl desired apply -f` accepts, so an export is a re-applyable,
human-readable backup. The writable contract (kinds, identity key shapes,
and the per-kind writable field sets) is pinned here from
`nintent/nautobot_intent_catalog/batch.py` (`KIND_ORDER`, `_KEYS`,
`_FIELDS`); the live acceptance check is that
`nctl desired export | nctl desired apply -f - --json` previews zero
changes.

Fidelity rules (state_bundle plan.md Step 1):

- every writable field is emitted explicitly, because apply is a partial
  upsert and an omitted field would be silently "preserved", masking an
  incomplete export;
- a snapshot row whose data the exporter cannot map onto the batch
  vocabulary (an unresolved reference, a snapshot model field this module
  does not know how to write, or a decode-time source issue that dropped or
  normalized row data) fails the export naming the problem instead of
  silently dropping data. Issues that leave every row's writable data
  intact — the NIC-readiness exclusions (whose rows are re-included from
  `unready_compute_instances`) and duplicate-MAC flags — do not block a
  faithful backup;
- operations are stable-sorted by writer kind order then identity, and
  free-form JSON values are key-sorted, so two exports of unchanged state
  are byte-identical (README_DEV lesson 5). Kind order equals the writer's
  dependency order, so the document also applies onto an empty database.
"""

from __future__ import annotations

import json
from typing import Any

import yaml
from pydantic import BaseModel

from nctl_core.config import Config, ConfigError
from nctl_core.compute.collection import READINESS_ISSUE_CODES
from nctl_core.compute.model import DesiredComputeInstance, DesiredComputePlatform
from nctl_core.nautobot import NautobotClient, NautobotError
from nctl_core.output import Envelope, EnvelopeError
from nctl_core.sources.desired import (
    DesiredAgent,
    DesiredEndpoint,
    DesiredEndpointRef,
    DesiredIPRange,
    DesiredNode,
    DesiredNodeOperationalOverride,
    DesiredService,
    DesiredServiceBinding,
    DesiredServicePlacement,
    DesiredSnapshot,
    DesiredWorkspace,
    fetch_desired_snapshot,
)

EXPORT_SCHEMA = "nctl.desired.export.v1"

# Writer dependency order, pinned from nintent batch.py KIND_ORDER.
KIND_ORDER = (
    "desired_node", "desired_ip_range", "desired_endpoint",
    "desired_compute_platform", "desired_compute_instance", "desired_service",
    "desired_service_placement", "desired_service_binding", "desired_node_operational_override",
    "desired_workspace", "desired_agent",
)

# Snapshot model fields each kind's emitter consumes (or deliberately treats
# as redundant transport denormalization). Any other field on a row model is
# desired data this exporter does not know how to write back — a named
# export failure, not a silent drop.
_HANDLED_MODEL_FIELDS: dict[str, tuple[type[BaseModel], frozenset[str]]] = {
    "desired_node": (DesiredNode, frozenset({
        "id", "slug", "name", "lifecycle", "node_type", "role",
        "accepted_actual_types", "expected_spec", "realized_device_id"})),
    "desired_ip_range": (DesiredIPRange, frozenset({
        "id", "name", "slug", "start_address", "end_address", "range_policy",
        "lifecycle", "generate_dnsmasq", "dnsmasq_options"})),
    "desired_endpoint": (DesiredEndpoint, frozenset({
        "id", "name", "endpoint_type", "node_id", "node_slug", "ip_address",
        "gateway_address", "ip_policy", "dns_name", "mdns_name", "vpn_dns_name",
        "mac_address", "protocol", "port", "generate_dnsmasq",
        "dnsmasq_record_type", "realized_ip_address_id"})),
    "desired_compute_platform": (DesiredComputePlatform, frozenset({
        "id", "name", "slug", "lifecycle", "control_node_id", "config",
        "realized_cluster_id"})),
    "desired_compute_instance": (DesiredComputeInstance, frozenset({
        "id", "desired_node_id", "platform_id", "instance_kind",
        "desired_power_state", "desired_presence", "vcpus", "memory_mb",
        "root_disk_gb", "config", "realized_vm_id"})),
    "desired_service": (DesiredService, frozenset({"id", "slug", "name", "lifecycle"})),
    "desired_service_placement": (DesiredServicePlacement, frozenset({
        "id", "service_id", "node_id", "endpoint_id", "instance_name",
        "desired_state", "deployment_profile", "management_mode",
        "config_schema_version", "config"})),
    "desired_service_binding": (DesiredServiceBinding, frozenset({
        "id", "binding_name", "consumer_placement_id", "provider_service_id",
        "provider_service_slug"})),
    "desired_node_operational_override": (DesiredNodeOperationalOverride, frozenset({
        "id", "node_id", "connection_path", "ansible_port", "power_control",
        "is_laptop", "local_endpoint", "tailscale_endpoint"})),
    "desired_workspace": (DesiredWorkspace, frozenset({
        "id", "slug", "name", "lifecycle", "source_remote_url", "expected_path",
        "desired_presence", "node_id", "node_slug"})),
    "desired_agent": (DesiredAgent, frozenset({
        "id", "slug", "name", "lifecycle", "zulip_user_id", "plane_user_id",
        "desired_zulip_channels", "workspace_id", "workspace_slug", "placement_id"})),
}


class DesiredExportData(BaseModel):
    document: dict[str, Any] = {}
    counts: dict[str, int] = {}
    operation_count: int = 0


class _ExportContext:
    def __init__(self, snapshot: DesiredSnapshot) -> None:
        self.errors: list[EnvelopeError] = []
        self.node_slugs = {node.id: node.slug for node in snapshot.nodes}
        self.service_slugs = {service.id: service.slug for service in snapshot.services}
        self.platform_slugs = {platform.id: platform.slug for platform in snapshot.compute_platforms}
        self.endpoint_identities = {
            endpoint.id: _endpoint_identity(endpoint.node_slug, endpoint.name, endpoint.endpoint_type)
            for endpoint in snapshot.endpoints
        }
        self.placement_identities = {
            placement.id: {
                "desired_service": self.service_slugs.get(placement.service_id),
                "instance_name": placement.instance_name,
            }
            for placement in snapshot.placements
        }

    def resolve(self, table: dict[str, Any], reference_id: str, *, kind: str, field: str, owner: str) -> Any:
        value = table.get(reference_id)
        if value is None:
            self.errors.append(EnvelopeError(
                code="unresolved_reference",
                message=f"{kind} {owner!r}: {field} references {reference_id!r}, which is not in this snapshot",
                detail={"kind": kind, "field": field, "reference_id": reference_id},
            ))
        return value


def _endpoint_identity(node_slug: str, name: str, endpoint_type: str) -> dict[str, str]:
    # Key member order must match the writer identity tuple for desired_endpoint.
    return {"desired_node": node_slug, "name": name, "endpoint_type": endpoint_type}


def _endpoint_ref_identity(ref: DesiredEndpointRef | None) -> dict[str, str] | None:
    if ref is None:
        return None
    return _endpoint_identity(ref.node_slug, ref.name, ref.endpoint_type)


def _canonical_free_form(value: Any) -> Any:
    """Key-sort free-form JSON values recursively for byte-stable output."""
    if isinstance(value, dict):
        return {key: _canonical_free_form(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonical_free_form(item) for item in value]
    return value


def export_document(snapshot: DesiredSnapshot) -> tuple[dict[str, Any], list[EnvelopeError]]:
    """Pure projection of a desired snapshot into the Phase 0 batch document."""
    errors = [
        EnvelopeError(
            code="desired_source_issue",
            message=(
                f"snapshot decode reported {issue.code} on {issue.target_kind} "
                f"{issue.target_slug_or_name or issue.target_id or '?'}: {issue.message} "
                "(rows may have been dropped or normalized; export cannot be a faithful backup)"
            ),
            detail=issue.model_dump(),
        )
        for issue in snapshot.source_issues
        if not _issue_preserves_row_data(issue)
    ]
    context = _ExportContext(snapshot)
    context.errors = errors

    operations: list[tuple[int, str, dict[str, Any]]] = []

    def add(kind: str, key: dict[str, Any], values: dict[str, Any]) -> None:
        operations.append((
            KIND_ORDER.index(kind),
            json.dumps(key, sort_keys=True, ensure_ascii=False),
            {"op": "upsert", "kind": kind, "key": key, "values": values},
        ))

    for node in snapshot.nodes:
        _check_handled_fields("desired_node", node, context)
        add("desired_node", {"slug": node.slug}, {
            "name": node.name,
            "slug": node.slug,
            "node_type": node.node_type,
            "lifecycle": node.lifecycle,
            "role": node.role,
            "accepted_actual_types": list(node.accepted_actual_types),
            "expected_spec": _canonical_free_form(node.expected_spec),
            "realized_device": node.realized_device_id,
        })

    for ip_range in snapshot.ip_ranges:
        _check_handled_fields("desired_ip_range", ip_range, context)
        add("desired_ip_range", {"slug": ip_range.slug}, {
            "name": ip_range.name,
            "slug": ip_range.slug,
            "start_address": ip_range.start_address,
            "end_address": ip_range.end_address,
            "range_policy": ip_range.range_policy,
            "lifecycle": ip_range.lifecycle,
            "generate_dnsmasq": ip_range.generate_dnsmasq,
            "dnsmasq_options": _canonical_free_form(ip_range.dnsmasq_options),
        })

    for endpoint in snapshot.endpoints:
        _check_handled_fields("desired_endpoint", endpoint, context)
        add("desired_endpoint", _endpoint_identity(endpoint.node_slug, endpoint.name, endpoint.endpoint_type), {
            "desired_node": endpoint.node_slug,
            "name": endpoint.name,
            "endpoint_type": endpoint.endpoint_type,
            "ip_address": endpoint.ip_address,
            "gateway_address": endpoint.gateway_address,
            "ip_policy": endpoint.ip_policy,
            "mac_address": endpoint.mac_address,
            "dns_name": endpoint.dns_name,
            "mdns_name": endpoint.mdns_name,
            "vpn_dns_name": endpoint.vpn_dns_name,
            "protocol": endpoint.protocol,
            "port": endpoint.port,
            "generate_dnsmasq": endpoint.generate_dnsmasq,
            "dnsmasq_record_type": endpoint.dnsmasq_record_type,
            "realized_ip_address": endpoint.realized_ip_address_id,
        })

    for platform in snapshot.compute_platforms:
        _check_handled_fields("desired_compute_platform", platform, context)
        control_node = context.resolve(
            context.node_slugs, platform.control_node_id,
            kind="desired_compute_platform", field="control_node", owner=platform.slug,
        )
        add("desired_compute_platform", {"slug": platform.slug}, {
            "name": platform.name,
            "slug": platform.slug,
            "lifecycle": platform.lifecycle,
            "control_node": control_node,
            "config": _canonical_free_form(platform.config),
            "realized_cluster": platform.realized_cluster_id,
        })

    for instance in [*snapshot.compute_instances, *snapshot.unready_compute_instances]:
        _check_handled_fields("desired_compute_instance", instance, context)
        node_slug = context.resolve(
            context.node_slugs, instance.desired_node_id,
            kind="desired_compute_instance", field="desired_node", owner=instance.id,
        )
        platform_slug = context.resolve(
            context.platform_slugs, instance.platform_id,
            kind="desired_compute_instance", field="platform", owner=instance.id,
        )
        add("desired_compute_instance", {"desired_node": node_slug}, {
            "desired_node": node_slug,
            "platform": platform_slug,
            "instance_kind": instance.instance_kind,
            "desired_power_state": instance.desired_power_state,
            "desired_presence": instance.desired_presence,
            "vcpus": instance.vcpus,
            "memory_mb": instance.memory_mb,
            "root_disk_gb": instance.root_disk_gb,
            "config": _canonical_free_form(instance.config),
            "realized_vm": instance.realized_vm_id,
        })

    for service in snapshot.services:
        _check_handled_fields("desired_service", service, context)
        add("desired_service", {"slug": service.slug}, {
            "name": service.name,
            "slug": service.slug,
            "lifecycle": service.lifecycle,
        })

    for placement in snapshot.placements:
        _check_handled_fields("desired_service_placement", placement, context)
        service_slug = context.resolve(
            context.service_slugs, placement.service_id,
            kind="desired_service_placement", field="desired_service", owner=placement.instance_name,
        )
        node_slug = context.resolve(
            context.node_slugs, placement.node_id,
            kind="desired_service_placement", field="desired_node", owner=placement.instance_name,
        )
        endpoint_identity = None
        if placement.endpoint_id is not None:
            endpoint_identity = context.resolve(
                context.endpoint_identities, placement.endpoint_id,
                kind="desired_service_placement", field="desired_endpoint", owner=placement.instance_name,
            )
        add("desired_service_placement",
            {"desired_service": service_slug, "instance_name": placement.instance_name}, {
                "desired_service": service_slug,
                "desired_node": node_slug,
                "desired_endpoint": endpoint_identity,
                "instance_name": placement.instance_name,
                "desired_state": placement.desired_state,
                "deployment_profile": placement.deployment_profile,
                "management_mode": placement.management_mode,
                "config_schema_version": placement.config_schema_version,
                "config": _canonical_free_form(placement.config),
            })

    for binding in snapshot.service_bindings:
        _check_handled_fields("desired_service_binding", binding, context)
        consumer_identity = context.resolve(
            context.placement_identities, binding.consumer_placement_id,
            kind="desired_service_binding", field="consumer_placement", owner=binding.binding_name,
        )
        add("desired_service_binding",
            {"consumer_placement": consumer_identity, "binding_name": binding.binding_name}, {
                "consumer_placement": consumer_identity,
                "binding_name": binding.binding_name,
                "provider_service": binding.provider_service_slug,
            })

    for override in snapshot.operational_overrides:
        _check_handled_fields("desired_node_operational_override", override, context)
        node_slug = context.resolve(
            context.node_slugs, override.node_id,
            kind="desired_node_operational_override", field="desired_node", owner=override.id,
        )
        add("desired_node_operational_override", {"desired_node": node_slug}, {
            "desired_node": node_slug,
            "connection_path": override.connection_path,
            "local_endpoint": _endpoint_ref_identity(override.local_endpoint),
            "tailscale_endpoint": _endpoint_ref_identity(override.tailscale_endpoint),
            "ansible_port": override.ansible_port,
            "power_control": override.power_control,
            "is_laptop": override.is_laptop,
        })

    for workspace in snapshot.workspaces:
        _check_handled_fields("desired_workspace", workspace, context)
        add("desired_workspace", {"slug": workspace.slug}, {
            "name": workspace.name,
            "slug": workspace.slug,
            "lifecycle": workspace.lifecycle,
            "source_remote_url": workspace.source_remote_url,
            "desired_node": workspace.node_slug,
            "expected_path": workspace.expected_path,
            "desired_presence": workspace.desired_presence,
        })

    for agent in snapshot.agents:
        _check_handled_fields("desired_agent", agent, context)
        placement_identity = None
        if agent.placement_id is not None:
            placement_identity = context.resolve(
                context.placement_identities, agent.placement_id,
                kind="desired_agent", field="desired_service_placement", owner=agent.slug,
            )
        add("desired_agent", {"slug": agent.slug}, {
            "name": agent.name,
            "slug": agent.slug,
            "lifecycle": agent.lifecycle,
            "desired_workspace": agent.workspace_slug,
            "desired_service_placement": placement_identity,
            "zulip_user_id": agent.zulip_user_id,
            "plane_user_id": agent.plane_user_id,
            "desired_zulip_channels": list(agent.desired_zulip_channels),
        })

    operations.sort(key=lambda item: (item[0], item[1]))
    document = {"dry_run": True, "operations": [item[2] for item in operations]}
    return document, context.errors


def _issue_preserves_row_data(issue: Any) -> bool:
    """Whether a source issue leaves every exported row's writable data intact.

    NIC-readiness exclusions are re-included via `unready_compute_instances`,
    and a duplicate desired MAC flags both rows without altering either.
    Everything else means a row was dropped or normalized.
    """
    if issue.code in READINESS_ISSUE_CODES and issue.target_kind == "compute_instance":
        return True
    return issue.code == "duplicate_mac_address"


def _check_handled_fields(kind: str, row: BaseModel, context: _ExportContext) -> None:
    _expected_model, handled = _HANDLED_MODEL_FIELDS[kind]
    unhandled = sorted(set(type(row).model_fields) - handled)
    if unhandled:
        context.errors.append(EnvelopeError(
            code="unexportable_field",
            message=(
                f"{kind} row carries snapshot field(s) this exporter cannot write back: "
                f"{', '.join(unhandled)} (extend desired_export.py alongside the batch contract)"
            ),
            detail={"kind": kind, "fields": unhandled},
        ))


def document_counts(document: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for operation in document.get("operations", []):
        counts[operation["kind"]] = counts.get(operation["kind"], 0) + 1
    return counts


def document_to_yaml(document: dict[str, Any]) -> str:
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=True, default_flow_style=False)


def build_desired_export(cfg: Config) -> Envelope[DesiredExportData]:
    try:
        token = cfg.nautobot.resolve_token()
    except ConfigError as exc:
        return Envelope.build(EXPORT_SCHEMA, DesiredExportData(), [EnvelopeError(code="nautobot_token_error", message=str(exc))])
    client = NautobotClient(cfg.nautobot.url, token)
    try:
        snapshot = fetch_desired_snapshot(client)
    except NautobotError as exc:
        return Envelope.build(EXPORT_SCHEMA, DesiredExportData(), [EnvelopeError(code="nautobot_error", message=str(exc))])
    finally:
        client.close()
    document, errors = export_document(snapshot)
    if errors:
        # A partial document must not look like a usable backup.
        return Envelope.build(EXPORT_SCHEMA, DesiredExportData(), errors)
    data = DesiredExportData(
        document=document,
        counts=document_counts(document),
        operation_count=len(document["operations"]),
    )
    return Envelope.build(EXPORT_SCHEMA, data, [])


def render_desired_export_text(envelope: Envelope[DesiredExportData]) -> str:
    if not envelope.ok:
        return "\n".join(f"error [{err.code}]: {err.message}" for err in envelope.errors)
    return document_to_yaml(envelope.data.document).rstrip("\n")
