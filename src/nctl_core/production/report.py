"""Pure translation of composition outcomes into report-schema node records."""

from __future__ import annotations

from typing import Any

from .model import NodeInput, NodeOutcome, PlacementInput

ACCEPTED_ACTUAL_TYPE_DEFAULTS = {
    "device": frozenset({"device"}),
    "virtual_machine": frozenset({"virtual_machine"}),
    "container": frozenset({"container"}),
    "service_host": frozenset({"device", "virtual_machine", "container"}),
}


def accepted_actual_types_source(node_type: str, accepted_actual_types: tuple[str, ...]) -> str:
    canonical = ACCEPTED_ACTUAL_TYPE_DEFAULTS.get(node_type)
    return "derived" if canonical is not None and frozenset(accepted_actual_types) == canonical else "override"


def build_node_report_record(node: NodeInput, outcome: NodeOutcome) -> dict[str, Any]:
    """Translate already-computed composition state without deriving inventory values."""
    placement_effects = [_placement_effect_entry(p, outcome) for p in sorted(node.placements, key=lambda p: p.instance_name)]
    return {
        "desired": {"node": {"id": node.id, "slug": node.slug, "name": node.name,
            "lifecycle": node.lifecycle, "node_type": node.node_type, "role": node.role,
            "accepted_actual_types": sorted(node.accepted_actual_types),
            "accepted_actual_types_source": accepted_actual_types_source(node.node_type, node.accepted_actual_types)},
            "endpoints": [{"id": e.id, "name": e.name, "endpoint_type": e.endpoint_type,
                "ip_address": e.ip_address, "dns_name": e.dns_name, "mdns_name": e.mdns_name}
                for e in sorted(node.endpoints, key=lambda item: item.id)],
            "placements": [_placement_desired_entry(p) for p in sorted(node.placements, key=lambda p: p.instance_name)],
            "operational_override": _operational_override_entry(node.operational_override)},
        "actual": {"operational_values": outcome.effective.as_dict() if outcome.effective is not None else {},
            "operational_finding": outcome.finding,
            "local_findings": ([{"code": outcome.local_error.code, "severity": "error",
                "message": outcome.local_error.message, "stage": outcome.local_error.stage,
                "evidence": outcome.local_error.evidence}] if outcome.local_error is not None else []),
            "service_dependencies": outcome.service_dependencies,
            "production": {"state": outcome.state, "reasons": outcome.reasons,
                "placement_effects": placement_effects}},
    }


def _placement_desired_entry(placement: PlacementInput) -> dict[str, Any]:
    return {"id": placement.id, "service_id": placement.service_id, "service_slug": placement.service_slug,
        "instance_name": placement.instance_name, "desired_state": placement.desired_state,
        "deployment_profile": placement.deployment_profile,
        "config_schema_version": placement.config_schema_version, "config": dict(placement.config),
        "endpoint_id": placement.endpoint_id}


def _operational_override_entry(override: Any | None) -> dict[str, Any] | None:
    if override is None:
        return None
    return {"id": override.id, "declared_host_os": override.declared_host_os,
        "connection_path": override.connection_path, "ansible_port": override.ansible_port,
        "power_control": override.power_control, "is_laptop": override.is_laptop,
        "local_endpoint_id": override.local_endpoint_id, "tailscale_endpoint_id": override.tailscale_endpoint_id}


def _placement_effect_entry(placement: PlacementInput, outcome: NodeOutcome) -> dict[str, Any]:
    if outcome.state == "included":
        effect, reason = ("applied", None) if placement.id in outcome.active_placement_ids else ("inactive_by_intent", None)
    elif placement.desired_state != "active":
        effect, reason = "inactive_by_intent", None
    else:
        reason = outcome.reasons[0] if outcome.reasons else ("node_out_of_scope" if outcome.state == "out_of_scope" else "production_unknown" if outcome.state == "unknown" else "node_skipped")
        effect = "not_applied"
    return {"placement_id": placement.id, "instance_name": placement.instance_name, "effect": effect, "reason": reason}
