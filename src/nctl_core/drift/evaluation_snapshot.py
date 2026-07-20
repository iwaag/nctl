"""Adapts a `SourceSnapshot` into `evaluation.py`'s pure functions (Phase 2 Step 4).

Mirrors `production/adapter.py`'s role for the composer: resolves the
relational lookups (`realized_device_id` -> `ActualDevice`, interfaces by
device id, a node's own evaluation feeding its endpoints' MAC-candidate
fallback) once per `SourceSnapshot`, so both the drift comparators
(`comparators.py`) and the dnsmasq MAC-source switch (`dnsmasq_query.py`)
compute the exact same evaluations from the same snapshot rather than
duplicating the resolution logic.

Node evaluations are computed first because `evaluate_endpoint_intent`'s
interface-candidate fallback reads a stored node evaluation's
`observed_facts.actual` when the node has no direct realized-device link
(the `node_evaluation=` parameter, ported unchanged from nintent).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timezone

from nctl_core.sources.actual import ActualInterface
from nctl_core.production.adapter import build_production_node_inputs
from nctl_core.production.derivation import DerivationFailure, resolve_operational_values
from nctl_core.sources.snapshot import SourceSnapshot

from .evaluation import EvaluationResult, evaluate_endpoint_intent, evaluate_node_intent, evaluate_service_intent
from .service_placement import evaluate_placement_drift


def evaluate_all_nodes(snapshot: SourceSnapshot) -> dict[str, EvaluationResult]:
    devices_by_id = {device.id: device for device in snapshot.actual.devices}
    vms_by_id = {vm.id: vm for vm in snapshot.actual.virtual_machines}
    interfaces_by_device_id = _interfaces_by_device_id(snapshot.actual.interfaces)

    results = {}
    for node in snapshot.desired.nodes:
        results[node.id] = evaluate_node_intent(
            node,
            device_candidates=snapshot.actual.devices,
            vm_candidates=snapshot.actual.virtual_machines,
            interfaces_by_device_id=interfaces_by_device_id,
            realized_device=devices_by_id.get(node.realized_device_id or ""),
            realized_vm=vms_by_id.get(node.realized_vm_id or ""),
        )
    return results


def evaluate_all_endpoints(
    snapshot: SourceSnapshot, node_evaluations: dict[str, EvaluationResult]
) -> dict[str, EvaluationResult]:
    nodes_by_id = {node.id: node for node in snapshot.desired.nodes}
    devices_by_id = {device.id: device for device in snapshot.actual.devices}
    vms_by_id = {vm.id: vm for vm in snapshot.actual.virtual_machines}
    ip_addresses_by_id = {ip.id: ip for ip in snapshot.actual.ip_addresses}
    interfaces_by_device_id = _interfaces_by_device_id(snapshot.actual.interfaces)

    results = {}
    for endpoint in snapshot.desired.endpoints:
        desired_node = nodes_by_id.get(endpoint.node_id)
        results[endpoint.id] = evaluate_endpoint_intent(
            endpoint,
            desired_node=desired_node,
            realized_ip=ip_addresses_by_id.get(endpoint.realized_ip_address_id or ""),
            ip_candidates=snapshot.actual.ip_addresses,
            range_candidates=snapshot.desired.ip_ranges,
            node_evaluation=node_evaluations.get(endpoint.node_id) if desired_node is not None else None,
            node_realized_device=devices_by_id.get(desired_node.realized_device_id or "") if desired_node else None,
            node_realized_vm=vms_by_id.get(desired_node.realized_vm_id or "") if desired_node else None,
            interfaces_by_device_id=interfaces_by_device_id,
        )
    return results


def evaluate_all_services(
    snapshot: SourceSnapshot,
    *,
    generated_at: str | None = None,
    stale_after_hours: int = 24,
) -> dict[str, EvaluationResult]:
    services_by_id = {service.id: service for service in snapshot.desired.services}
    dependencies_by_service: dict[str, list] = defaultdict(list)
    for dependency in snapshot.desired.dependencies:
        dependencies_by_service[dependency.source_service_id].append(dependency)

    nodes_by_id = {node.id: node for node in snapshot.desired.nodes}
    devices_by_id = {device.id: device for device in snapshot.actual.devices}
    effective_by_node = {}
    operation_generated_at = generated_at or snapshot.fetched_at.isoformat()
    for node_input in build_production_node_inputs(snapshot):
        try:
            effective_by_node[node_input.id] = resolve_operational_values(
                node_id=node_input.id,
                node_slug=node_input.slug,
                endpoints=node_input.endpoints,
                override=node_input.operational_override,
                realized_type=node_input.realized.realized_type if node_input.realized else None,
                facts=node_input.realized.facts if node_input.realized else None,
                generated_at=operation_generated_at,
            )
        except DerivationFailure:
            continue
    placement_rows = []
    for placement in snapshot.desired.placements:
        if placement.desired_state != "active":
            continue
        node = nodes_by_id.get(placement.node_id)
        effective = effective_by_node.get(placement.node_id)
        placement_rows.append(
            {
                "placement_id": placement.id,
                "service_id": placement.service_id,
                "node_id": placement.node_id,
                "node_slug": node.slug if node else None,
                "instance_name": placement.instance_name,
                "deployment_profile": placement.deployment_profile,
                "realized_device_id": node.realized_device_id if node else None,
                "actual_state_policy": effective.actual_state_policy.value if effective else None,
                "host_os": effective.host_os.value if effective else None,
            }
        )
    device_facts = {
        device.id: {
            "observed_system": device.actual_facts().observed_system,
            "observed_services": device.actual_facts().observed_services,
            "service_inventory_updated_at": device.actual_facts().service_inventory_updated_at,
        }
        for device in snapshot.actual.devices
    }
    placement_report = evaluate_placement_drift(
        [{"id": service.id, "name": service.name} for service in snapshot.desired.services],
        placement_rows,
        device_facts,
        {device.id: node.id for node in snapshot.desired.nodes for device in [devices_by_id.get(node.realized_device_id or "")] if device},
        now=_parse_now(generated_at),
        stale_after_hours=stale_after_hours,
    )

    results = {}
    for service in snapshot.desired.services:
        base = evaluate_service_intent(
            service,
            dependencies=dependencies_by_service.get(service.id, ()),
            resolved_services_by_id=services_by_id,
            observed_facts={},
        )
        observation = placement_report[service.id]
        gaps = list(base.gap_summary.get("gaps", []))
        if observation["status"] == "no_active_placement":
            gaps.append({"code": "service_has_no_active_placement", "severity": "needs_review"})
        for placement in observation["placements"]:
            grouped: dict[str, list[dict]] = defaultdict(list)
            for finding in placement["gaps"]:
                grouped[finding["code"]].append(finding)
            for code, findings in sorted(grouped.items()):
                gaps.append(
                    {
                        "code": code,
                        "severity": "unknown" if code == "service_observation_missing" else "missing",
                        "expected": _placement_evidence(placement),
                        "actual": {"findings": findings, **_observed_evidence(placement)},
                    }
                )
        for unexpected in observation["unexpected_locations"]:
            gaps.append(
                {
                    "code": unexpected["code"],
                    "severity": "conflict",
                    "expected": {"service_id": service.id, "observed_key": observation["observed_key"]},
                    "actual": unexpected,
                }
            )
        status = _status_from_gaps(gaps)
        summary = dict(base.deterministic_summary)
        summary.update(
            status=status,
            gap_codes=[gap["code"] for gap in gaps],
            service_observation_status=observation["status"],
            evaluation_scope="service_lifecycle_dependencies_and_placements",
        )
        results[service.id] = replace(
            base,
            status=status,
            deterministic_summary=summary,
            observed_facts={"service_observation_status": observation["status"], "placement_observations": observation},
            gap_summary={"gaps": gaps},
        )
    return results


def _parse_now(value: str | None) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else datetime.now(timezone.utc)
    except ValueError:
        parsed = datetime.now(timezone.utc)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _placement_evidence(placement: dict) -> dict:
    return {
        key: placement.get(key)
        for key in ("placement_id", "node_id", "node_slug", "deployment_profile", "realized_device_id", "observed_key")
    }


def _observed_evidence(placement: dict) -> dict:
    return {
        key: placement.get(key)
        for key in ("observed_state", "observed_source", "observed_endpoint", "observed_checked_at", "observed_at")
        if placement.get(key) is not None
    }


def _status_from_gaps(gaps: list[dict]) -> str:
    severities = {gap.get("severity") for gap in gaps}
    for severity in ("conflict", "missing", "partial", "needs_review", "unknown"):
        if severity in severities:
            return severity
    return "satisfied"


def _interfaces_by_device_id(interfaces: list[ActualInterface]) -> dict[str, list[ActualInterface]]:
    grouped: dict[str, list[ActualInterface]] = defaultdict(list)
    for interface in interfaces:
        if interface.device_id:
            grouped[interface.device_id].append(interface)
    return dict(grouped)
