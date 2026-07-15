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

from nctl_core.sources.actual import ActualInterface
from nctl_core.sources.snapshot import SourceSnapshot

from .evaluation import EvaluationResult, evaluate_endpoint_intent, evaluate_node_intent, evaluate_service_intent


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


def evaluate_all_services(snapshot: SourceSnapshot) -> dict[str, EvaluationResult]:
    services_by_id = {service.id: service for service in snapshot.desired.services}
    dependencies_by_service: dict[str, list] = defaultdict(list)
    for dependency in snapshot.desired.dependencies:
        dependencies_by_service[dependency.source_service_id].append(dependency)

    results = {}
    for service in snapshot.desired.services:
        results[service.id] = evaluate_service_intent(
            service,
            dependencies=dependencies_by_service.get(service.id, ()),
            resolved_services_by_id=services_by_id,
        )
    return results


def _interfaces_by_device_id(interfaces: list[ActualInterface]) -> dict[str, list[ActualInterface]]:
    grouped: dict[str, list[ActualInterface]] = defaultdict(list)
    for interface in interfaces:
        if interface.device_id:
            grouped[interface.device_id].append(interface)
    return dict(grouped)
