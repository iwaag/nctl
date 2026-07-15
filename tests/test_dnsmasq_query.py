from __future__ import annotations

from datetime import datetime, timezone

from nctl_core.dnsmasq_query import dnsmasq_inputs_from_snapshot
from nctl_core.sources.actual import ActualDevice, ActualInterface, ActualSnapshot
from nctl_core.sources.desired import DesiredEndpoint, DesiredIPRange, DesiredNode, DesiredSnapshot
from nctl_core.sources.snapshot import SourceSnapshot


def make_snapshot(*, nodes=(), endpoints=(), ip_ranges=(), devices=(), interfaces=()) -> SourceSnapshot:
    return SourceSnapshot(
        desired=DesiredSnapshot(nodes=list(nodes), endpoints=list(endpoints), ip_ranges=list(ip_ranges)),
        actual=ActualSnapshot(devices=list(devices), interfaces=list(interfaces)),
        fetched_at=datetime.now(timezone.utc),
    )


def test_dnsmasq_inputs_from_snapshot_shapes_endpoints_and_ip_ranges():
    node = DesiredNode(id="node-1", slug="edge-1", name="Edge 1", lifecycle="active", node_type="device")
    endpoint = DesiredEndpoint(
        id="endpoint-1", name="primary", endpoint_type="primary", node_id="node-1", node_slug="edge-1",
        ip_address="192.0.2.10/32", ip_policy="dhcp_reserved", dns_name="edge-1.example.test",
        mdns_name="edge-1.local", generate_dnsmasq=True, dnsmasq_record_type="host_record",
    )
    ip_range = DesiredIPRange(
        id="range-1", name="dynamic", slug="dynamic", start_address="192.168.0.200", end_address="192.168.0.250",
        range_policy="dhcp_dynamic_pool", lifecycle="active", generate_dnsmasq=True, dnsmasq_options={"lease_time": "12h"},
    )
    snapshot = make_snapshot(nodes=[node], endpoints=[endpoint], ip_ranges=[ip_range])

    result = dnsmasq_inputs_from_snapshot(snapshot)

    endpoint_row = result.endpoints[0]
    assert endpoint_row["endpoint_type"] == "primary"
    assert endpoint_row["ip_policy"] == "dhcp_reserved"
    assert endpoint_row["dnsmasq_record_type"] == "host_record"
    assert endpoint_row["desired_node"] == {"id": "node-1", "name": "Edge 1", "slug": "edge-1", "lifecycle": "active"}

    ip_range_row = result.ip_ranges[0]
    assert ip_range_row["range_policy"] == "dhcp_dynamic_pool"
    assert ip_range_row["lifecycle"] == "active"
    assert ip_range_row["dnsmasq_options"] == {"lease_time": "12h"}


def test_dnsmasq_inputs_from_snapshot_falls_back_to_ids_when_node_missing():
    endpoint = DesiredEndpoint(
        id="endpoint-1", name="primary", endpoint_type="primary", node_id="ghost-node", node_slug="ghost",
        ip_address=None, ip_policy="static", dns_name=None,
    )
    snapshot = make_snapshot(endpoints=[endpoint])

    result = dnsmasq_inputs_from_snapshot(snapshot)

    assert result.endpoints[0]["desired_node"] == {"id": "ghost-node", "slug": "ghost"}


def test_dnsmasq_inputs_from_snapshot_computes_evaluations_fresh():
    device = ActualDevice(id="dev-1", name="edge-1.local")
    interface = ActualInterface(id="iface-1", name="eth0", mac_address="aa:bb:cc:dd:ee:ff", device_id="dev-1")
    node = DesiredNode(id="node-1", slug="edge-1", name="edge-1", lifecycle="active", node_type="device", realized_device_id="dev-1")
    endpoint = DesiredEndpoint(
        id="endpoint-1", name="primary", endpoint_type="primary", node_id="node-1", node_slug="edge-1",
        ip_address="192.0.2.10/32", ip_policy="static", dns_name="edge-1.example.test", generate_dnsmasq=True,
    )
    snapshot = make_snapshot(nodes=[node], endpoints=[endpoint], devices=[device], interfaces=[interface])

    result = dnsmasq_inputs_from_snapshot(snapshot)

    assert "node-1" in result.node_evaluations
    assert result.node_evaluations["node-1"]["actual_refs"][0]["object_type"] == "dcim.device"
    assert "endpoint-1" in result.endpoint_evaluations
    assert result.endpoint_evaluations["endpoint-1"]["observed_facts"]["dhcp_mac_candidates"][0]["mac_address"] == "aa:bb:cc:dd:ee:ff"
