"""Adapts a `SourceSnapshot` into the composer's input dataclasses (Phase 2 Step 2).

Mirrors nintent's `jobs.py::_build_production_node_inputs`, reading Phase 2
Step 1's typed read-models instead of the ORM. All node endpoints and the
optional override are passed separately so the pure resolver, not this
GraphQL adapter, owns selection and precedence.
"""

from __future__ import annotations

from collections import defaultdict

from nctl_core.sources.actual import ActualDevice, read_actual_facts
from nctl_core.sources.desired import (
    DesiredEndpoint,
    DesiredNode,
    DesiredNodeOperationalOverride,
    DesiredService,
    DesiredServicePlacement,
)
from nctl_core.sources.snapshot import SourceSnapshot

from .composer import NodeInput, PlacementInput, RealizedState
from .derivation import EndpointCandidate, OperationalOverride


def build_production_node_inputs(snapshot: SourceSnapshot) -> list[NodeInput]:
    override_by_node = {item.node_id: item for item in snapshot.desired.operational_overrides}
    endpoints_by_node: dict[str, list[DesiredEndpoint]] = defaultdict(list)
    for endpoint in snapshot.desired.endpoints:
        endpoints_by_node[endpoint.node_id].append(endpoint)
    placements_by_node: dict[str, list[DesiredServicePlacement]] = defaultdict(list)
    for placement in snapshot.desired.placements:
        placements_by_node[placement.node_id].append(placement)
    devices_by_id = {device.id: device for device in snapshot.actual.devices}
    services_by_id = {service.id: service for service in snapshot.desired.services}

    node_inputs = []
    for node in sorted(snapshot.desired.nodes, key=lambda n: n.slug):
        placements = tuple(
            _placement_input(placement, services_by_id)
            for placement in sorted(placements_by_node.get(node.id, ()), key=lambda p: p.instance_name)
        )
        node_inputs.append(
            NodeInput(
                id=node.id,
                slug=node.slug,
                name=node.name,
                lifecycle=node.lifecycle,
                node_type=node.node_type,
                role=node.role,
                accepted_actual_types=tuple(node.accepted_actual_types),
                endpoints=tuple(
                    _endpoint_candidate(endpoint)
                    for endpoint in sorted(endpoints_by_node.get(node.id, ()), key=lambda item: item.id)
                ),
                operational_override=_operational_override(override_by_node.get(node.id)),
                placements=placements,
                realized=_realized_state(node, devices_by_id),
            )
        )
    return node_inputs


def _endpoint_candidate(endpoint: DesiredEndpoint) -> EndpointCandidate:
    return EndpointCandidate(
        id=endpoint.id,
        name=endpoint.name,
        endpoint_type=endpoint.endpoint_type,
        node_slug=endpoint.node_slug,
        ip_address=endpoint.ip_address,
        dns_name=endpoint.dns_name,
        mdns_name=endpoint.mdns_name,
    )


def _operational_override(item: DesiredNodeOperationalOverride | None) -> OperationalOverride | None:
    if item is None:
        return None
    return OperationalOverride(
        id=item.id,
        declared_host_os=item.declared_host_os,
        connection_path=item.connection_path,
        local_endpoint_id=item.local_endpoint.id if item.local_endpoint else None,
        tailscale_endpoint_id=item.tailscale_endpoint.id if item.tailscale_endpoint else None,
        ansible_port=item.ansible_port,
        power_control=item.power_control,
        is_laptop=item.is_laptop,
    )


def _placement_input(
    placement: DesiredServicePlacement, services_by_id: dict[str, DesiredService]
) -> PlacementInput:
    service = services_by_id.get(placement.service_id)
    return PlacementInput(
        id=placement.id,
        instance_name=placement.instance_name,
        deployment_profile=placement.deployment_profile,
        config_schema_version=placement.config_schema_version,
        desired_state=placement.desired_state,
        config=placement.config,
        service_id=placement.service_id,
        service_slug=service.slug if service is not None else "",
        instance_role=placement.instance_role,
        assignment_source=placement.assignment_source,
        endpoint_id=placement.endpoint_id,
    )


def _realized_state(node: DesiredNode, devices_by_id: dict[str, ActualDevice]) -> RealizedState | None:
    if node.realized_device_id:
        device = devices_by_id.get(node.realized_device_id)
        facts = device.actual_facts() if device is not None else read_actual_facts({})
        return RealizedState(realized_type="device", facts=facts, nautobot_device_id=node.realized_device_id)
    if node.realized_vm_id:
        # Schema 1.0 supports nodeutils-backed Devices only; a realized VM is
        # surfaced to the composer so it is skipped with unsupported_actual_type.
        return RealizedState(realized_type="virtual_machine", facts=read_actual_facts({}))
    return None
