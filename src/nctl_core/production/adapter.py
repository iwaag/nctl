"""Adapts a `SourceSnapshot` into the composer's input dataclasses (Phase 2 Step 2).

Mirrors nintent's `jobs.py::_build_production_node_inputs`, reading Phase 2
Step 1's typed read-models instead of the ORM. `DesiredNodeOperationalConfig`'s
`local_endpoint`/`tailscale_endpoint` are already shaped like `EndpointInput`
(same fields, ported by the same fetch layer), so mapping them across is a
field-for-field copy rather than a second GraphQL round trip.
"""

from __future__ import annotations

from collections import defaultdict

from nctl_core.sources.actual import ActualDevice, read_actual_facts
from nctl_core.sources.desired import (
    DesiredEndpointRef,
    DesiredNode,
    DesiredNodeOperationalConfig,
    DesiredServicePlacement,
)
from nctl_core.sources.snapshot import SourceSnapshot

from .composer import EndpointInput, NodeInput, OperationalConfigInput, PlacementInput, RealizedState


def build_production_node_inputs(snapshot: SourceSnapshot) -> list[NodeInput]:
    operational_by_node = {oc.node_id: oc for oc in snapshot.desired.operational_configs}
    placements_by_node: dict[str, list[DesiredServicePlacement]] = defaultdict(list)
    for placement in snapshot.desired.placements:
        placements_by_node[placement.node_id].append(placement)
    devices_by_id = {device.id: device for device in snapshot.actual.devices}

    node_inputs = []
    for node in sorted(snapshot.desired.nodes, key=lambda n: n.slug):
        placements = tuple(
            _placement_input(placement)
            for placement in sorted(placements_by_node.get(node.id, ()), key=lambda p: p.instance_name)
        )
        node_inputs.append(
            NodeInput(
                id=node.id,
                slug=node.slug,
                name=node.name,
                lifecycle=node.lifecycle,
                node_type=node.node_type,
                operational_config=_operational_config_input(operational_by_node.get(node.id)),
                placements=placements,
                realized=_realized_state(node, devices_by_id),
            )
        )
    return node_inputs


def _endpoint_input(ref: DesiredEndpointRef | None) -> EndpointInput | None:
    if ref is None:
        return None
    return EndpointInput(
        name=ref.name,
        endpoint_type=ref.endpoint_type,
        node_slug=ref.node_slug,
        ip_address=ref.ip_address,
        dns_name=ref.dns_name,
        mdns_name=ref.mdns_name,
    )


def _operational_config_input(oc: DesiredNodeOperationalConfig | None) -> OperationalConfigInput | None:
    if oc is None:
        return None
    return OperationalConfigInput(
        id=oc.id,
        actual_state_policy=oc.actual_state_policy,
        connection_path=oc.connection_path,
        power_control=oc.power_control,
        is_laptop=oc.is_laptop,
        expected_host_os=oc.expected_host_os,
        declared_host_os=oc.declared_host_os,
        local_endpoint=_endpoint_input(oc.local_endpoint),
        tailscale_endpoint=_endpoint_input(oc.tailscale_endpoint),
        ansible_port=oc.ansible_port,
    )


def _placement_input(placement: DesiredServicePlacement) -> PlacementInput:
    return PlacementInput(
        id=placement.id,
        instance_name=placement.instance_name,
        deployment_profile=placement.deployment_profile,
        config_schema_version=placement.config_schema_version,
        desired_state=placement.desired_state,
        config=placement.config,
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
