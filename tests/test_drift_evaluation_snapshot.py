from __future__ import annotations

from datetime import datetime, timezone

from nctl_core.drift.evaluation_snapshot import evaluate_all_endpoints, evaluate_all_nodes, evaluate_all_services
from nctl_core.sources.actual import ActualDevice, ActualInterface, ActualIPAddress, ActualSnapshot
from nctl_core.sources.desired import (
    DesiredDependency,
    DesiredEndpoint,
    DesiredNode,
    DesiredNodeOperationalConfig,
    DesiredService,
    DesiredServicePlacement,
    DesiredSnapshot,
)
from nctl_core.sources.snapshot import SourceSnapshot


def make_snapshot(*, nodes=(), endpoints=(), services=(), dependencies=(), placements=(), operational_configs=(), devices=(), interfaces=(), ip_addresses=()) -> SourceSnapshot:
    return SourceSnapshot(
        desired=DesiredSnapshot(nodes=list(nodes), endpoints=list(endpoints), services=list(services), dependencies=list(dependencies), placements=list(placements), operational_configs=list(operational_configs)),
        actual=ActualSnapshot(devices=list(devices), interfaces=list(interfaces), ip_addresses=list(ip_addresses)),
        fetched_at=datetime.now(timezone.utc),
    )


def test_evaluate_all_nodes_resolves_realized_device_by_id():
    device = ActualDevice(id="dev-1", name="agweb.local", serial="SER1")
    node = DesiredNode(
        id="n1", slug="agweb", name="agweb", lifecycle="active", node_type="device",
        expected_spec={"serial": "SER1"}, realized_device_id="dev-1",
    )
    snapshot = make_snapshot(nodes=[node], devices=[device])

    evaluations = evaluate_all_nodes(snapshot)

    assert evaluations["n1"].status == "satisfied"
    assert evaluations["n1"].actual_refs[0]["id"] == "dev-1"


def test_evaluate_all_endpoints_uses_matching_node_evaluation_for_mac_candidates():
    device = ActualDevice(id="dev-1", name="agweb.local")
    interface = ActualInterface(id="iface-1", name="eth0", mac_address="aa:bb:cc:dd:ee:ff", device_id="dev-1")
    ip = ActualIPAddress(id="ip-1", host="192.0.2.10", mask_length=32)
    node = DesiredNode(id="n1", slug="agweb", name="agweb", lifecycle="active", node_type="device", realized_device_id="dev-1")
    endpoint = DesiredEndpoint(
        id="e1", name="primary", endpoint_type="primary", node_id="n1", node_slug="agweb",
        ip_address="192.0.2.10/32", ip_policy="dhcp_reserved", dns_name="agweb.example.test",
        generate_dnsmasq=True, realized_ip_address_id="ip-1",
    )
    snapshot = make_snapshot(nodes=[node], endpoints=[endpoint], devices=[device], interfaces=[interface], ip_addresses=[ip])

    node_evaluations = evaluate_all_nodes(snapshot)
    endpoint_evaluations = evaluate_all_endpoints(snapshot, node_evaluations)

    result = endpoint_evaluations["e1"]
    # No DesiredIPRange exists in this snapshot, so the endpoint's
    # `dhcp_reserved` policy has no matching pool -> `missing_ip_policy_range`
    # keeps this `partial`, not `satisfied`; the MAC candidate itself still
    # resolves correctly, which is what this test is really checking.
    assert result.status == "partial"
    assert "missing_ip_policy_range" in result.deterministic_summary["gap_codes"]
    assert result.deterministic_summary["dhcp_reservation_ready"] is False
    assert result.observed_facts["dhcp_mac_candidates"][0]["mac_address"] == "aa:bb:cc:dd:ee:ff"


def test_evaluate_all_endpoints_handles_endpoint_with_no_matching_node():
    endpoint = DesiredEndpoint(
        id="e1", name="primary", endpoint_type="primary", node_id="missing-node", node_slug="ghost",
        ip_address="192.0.2.10/32", ip_policy="static", dns_name="ghost.example.test",
    )
    snapshot = make_snapshot(endpoints=[endpoint])

    result = evaluate_all_endpoints(snapshot, {})["e1"]

    assert result.observed_facts["interface_candidates"] == []


def test_evaluate_all_services_resolves_dependency_by_source_service_id():
    dependency = DesiredDependency(
        id="d1", source_service_id="s1", dependency_kind="component", namespace="default",
        name="database", raw_ref="component:default/database", dependency_type="component",
        resolution_status="unresolved",
    )
    service = DesiredService(
        id="s1", slug="api", name="api", display_name="API", service_type="service", lifecycle="active",
        catalog_namespace="default", catalog_metadata_name="api",
    )
    snapshot = make_snapshot(services=[service], dependencies=[dependency])

    result = evaluate_all_services(snapshot)["s1"]

    assert result.status == "partial"
    assert result.deterministic_summary["dependency_counts"]["unresolved"] == 1


def test_evaluate_all_services_uses_placement_device_observation() -> None:
    service = DesiredService(
        id="s1", slug="nomad", name="nomad", display_name="Nomad", service_type="service",
        lifecycle="active", catalog_namespace="default", catalog_metadata_name="nomad",
    )
    node = DesiredNode(
        id="n1", slug="agnomad", name="agnomad", lifecycle="active", node_type="device",
        realized_device_id="dev-1",
    )
    placement = DesiredServicePlacement(
        id="p1", service_id="s1", node_id="n1", instance_name="nomad",
        deployment_profile="nomad_server", config_schema_version="v1",
    )
    operational = DesiredNodeOperationalConfig(
        id="oc1", node_id="n1", actual_state_policy="observed", expected_host_os="linux",
        connection_path="local",
    )
    device = ActualDevice(
        id="dev-1", name="agnomad",
        facts={
            "host_system": "Linux",
            "service_inventory_updated_at": "2026-07-16T00:30:00+00:00",
            "observed_services": {"nomad": {"state": "running", "source": "systemd", "checked_at": "2026-07-16T00:30:00+00:00"}},
        },
    )
    snapshot = make_snapshot(
        nodes=[node], services=[service], placements=[placement], operational_configs=[operational], devices=[device]
    )

    result = evaluate_all_services(
        snapshot, generated_at="2026-07-16T01:00:00+00:00", stale_after_hours=24
    )["s1"]

    assert result.status == "satisfied"
    assert result.gap_summary == {"gaps": []}
    assert result.observed_facts["placement_observations"]["placements"][0]["observed_state"] == "running"
