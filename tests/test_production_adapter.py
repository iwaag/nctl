from __future__ import annotations

from datetime import datetime, timezone

from nctl_core.production.adapter import build_production_node_inputs
from nctl_core.sources.actual import ActualDevice, ActualSnapshot
from nctl_core.sources.desired import (
    DesiredEndpointRef,
    DesiredNode,
    DesiredNodeOperationalConfig,
    DesiredServicePlacement,
    DesiredSnapshot,
)
from nctl_core.sources.snapshot import SourceSnapshot


def make_snapshot(*, nodes, operational_configs=(), placements=(), devices=()) -> SourceSnapshot:
    return SourceSnapshot(
        desired=DesiredSnapshot(
            nodes=list(nodes),
            operational_configs=list(operational_configs),
            placements=list(placements),
        ),
        actual=ActualSnapshot(devices=list(devices)),
        fetched_at=datetime.now(timezone.utc),
    )


def test_build_production_node_inputs_joins_operational_config_placements_and_realized_device():
    node = DesiredNode(
        id="node-1", slug="agweb", name="agweb", lifecycle="active", node_type="device", realized_device_id="dev-1"
    )
    op_config = DesiredNodeOperationalConfig(
        id="opconf-1",
        node_id="node-1",
        actual_state_policy="required",
        connection_path="local",
        expected_host_os="linux",
        local_endpoint=DesiredEndpointRef(
            id="endpoint-1", name="primary", endpoint_type="primary", node_slug="agweb", ip_address="192.0.2.10/32"
        ),
    )
    placement = DesiredServicePlacement(
        id="placement-1",
        service_id="service-1",
        node_id="node-1",
        instance_name="dnsmasq-main",
        deployment_profile="dnsmasq",
        config_schema_version="1",
        config={"enable_dhcp": True},
    )
    device = ActualDevice(
        id="dev-1",
        name="agbach.local",
        facts={
            "host_system": "linux",
            "primary_mac_address": "aa:bb:cc:dd:ee:ff",
            "last_seen": "2026-07-14T00:00:00+00:00",
        },
    )
    snapshot = make_snapshot(
        nodes=[node], operational_configs=[op_config], placements=[placement], devices=[device]
    )

    [node_input] = build_production_node_inputs(snapshot)

    assert node_input.slug == "agweb"
    assert node_input.operational_config is not None
    assert node_input.operational_config.expected_host_os == "linux"
    assert node_input.operational_config.local_endpoint is not None
    assert node_input.operational_config.local_endpoint.node_slug == "agweb"
    assert len(node_input.placements) == 1
    assert node_input.placements[0].instance_name == "dnsmasq-main"
    assert node_input.realized is not None
    assert node_input.realized.realized_type == "device"
    assert node_input.realized.nautobot_device_id == "dev-1"
    assert node_input.realized.facts.observed_system == "linux"
    assert node_input.realized.facts.mac_address == "aa:bb:cc:dd:ee:ff"


def test_build_production_node_inputs_handles_no_operational_config_or_placements():
    node = DesiredNode(id="node-2", slug="agempty", name="agempty", lifecycle="planned", node_type="device")
    snapshot = make_snapshot(nodes=[node])

    [node_input] = build_production_node_inputs(snapshot)

    assert node_input.operational_config is None
    assert node_input.placements == ()
    assert node_input.realized is None


def test_build_production_node_inputs_realized_vm_has_no_device_lookup():
    node = DesiredNode(
        id="node-3", slug="agvm", name="agvm", lifecycle="active", node_type="virtual_machine", realized_vm_id="vm-1"
    )
    snapshot = make_snapshot(nodes=[node])

    [node_input] = build_production_node_inputs(snapshot)

    assert node_input.realized is not None
    assert node_input.realized.realized_type == "virtual_machine"
    assert node_input.realized.nautobot_device_id is None


def test_build_production_node_inputs_sorts_by_slug():
    nodes = [
        DesiredNode(id="node-z", slug="agz", name="agz", lifecycle="active", node_type="device"),
        DesiredNode(id="node-a", slug="aga", name="aga", lifecycle="active", node_type="device"),
    ]
    snapshot = make_snapshot(nodes=nodes)

    result = build_production_node_inputs(snapshot)

    assert [n.slug for n in result] == ["aga", "agz"]
