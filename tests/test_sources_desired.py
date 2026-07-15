from __future__ import annotations

import httpx
import respx

from nctl_core.nautobot import NautobotClient
from nctl_core.sources.desired import DESIRED_QUERY, fetch_desired_snapshot

BASE_URL = "http://nautobot.test"


def _endpoint_ref(node_slug: str, name: str = "local") -> dict:
    return {
        "id": f"endpoint-{name}",
        "name": name,
        "endpoint_type": "PRIMARY",
        "ip_address": "192.0.2.10/32",
        "dns_name": None,
        "mdns_name": f"{name}.local",
        "desired_node": {"slug": node_slug},
    }


@respx.mock
def test_fetch_desired_snapshot_lowercases_choice_fields_and_flattens_relations():
    respx.post(f"{BASE_URL}/api/graphql/").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "desired_nodes": [
                        {
                            "id": "node-1",
                            "slug": "edge-1",
                            "name": "Edge 1",
                            "lifecycle": "APPROVED",
                            "node_type": "DEVICE",
                            "role": None,
                            "realized_device": {"id": "dev-1"},
                            "realized_vm": None,
                        }
                    ],
                    "desired_endpoints": [
                        {
                            "id": "endpoint-1",
                            "name": "primary",
                            "endpoint_type": "PRIMARY",
                            "ip_address": "192.0.2.10/32",
                            "ip_policy": "DHCP_RESERVED",
                            "dns_name": "edge-1.example.test",
                            "mdns_name": "edge-1.local",
                            "vpn_dns_name": None,
                            "protocol": None,
                            "port": None,
                            "generate_dnsmasq": True,
                            "dnsmasq_record_type": "HOST_RECORD",
                            "desired_node": {"id": "node-1", "slug": "edge-1"},
                        }
                    ],
                    "desired_ip_ranges": [
                        {
                            "id": "range-1",
                            "name": "dynamic",
                            "slug": "dynamic",
                            "start_address": "192.168.0.200",
                            "end_address": "192.168.0.250",
                            "range_policy": "DHCP_DYNAMIC_POOL",
                            "lifecycle": "ACTIVE",
                            "generate_dnsmasq": True,
                            "dnsmasq_options": {"lease_time": "12h"},
                        }
                    ],
                    "desired_node_operational_configs": [
                        {
                            "id": "opconf-1",
                            "desired_node": {"id": "node-1"},
                            "actual_state_policy": "REQUIRED",
                            "expected_host_os": "LINUX",
                            "declared_host_os": None,
                            "connection_path": "LOCAL",
                            "ansible_port": None,
                            "power_control": "WOL",
                            "is_laptop": False,
                            "local_endpoint": _endpoint_ref("edge-1"),
                            "tailscale_endpoint": None,
                        }
                    ],
                    "desired_service_placements": [
                        {
                            "id": "placement-1",
                            "desired_service": {"id": "service-1"},
                            "desired_node": {"id": "node-1"},
                            "desired_endpoint": None,
                            "instance_name": "dnsmasq-main",
                            "desired_state": "ACTIVE",
                            "instance_role": None,
                            "deployment_profile": "dnsmasq",
                            "config_schema_version": "1",
                            "config": {"enable_dhcp": True},
                            "assignment_source": "MANUAL",
                        }
                    ],
                    "desired_services": [
                        {
                            "id": "service-1",
                            "slug": "dnsmasq-service",
                            "name": "dnsmasq-service",
                            "display_name": "dnsmasq",
                            "service_type": "SERVICE",
                            "lifecycle": "ACTIVE",
                            "catalog_namespace": "default",
                            "catalog_metadata_name": "dnsmasq",
                            "requirements": {},
                            "placement_policy": {},
                        }
                    ],
                    "desired_dependencies": [
                        {
                            "id": "dependency-1",
                            "source_service": {"id": "service-1"},
                            "dependency_kind": "requires",
                            "namespace": "default",
                            "name": "postgres",
                            "raw_ref": "default/postgres",
                            "dependency_type": "service",
                            "resolution_status": "UNRESOLVED",
                            "resolved_service": None,
                        }
                    ],
                }
            },
        )
    )
    client = NautobotClient(BASE_URL, "tok")

    snapshot = fetch_desired_snapshot(client)

    node = snapshot.nodes[0]
    assert node.lifecycle == "approved"
    assert node.node_type == "device"
    assert node.realized_device_id == "dev-1"
    assert node.realized_vm_id is None

    endpoint = snapshot.endpoints[0]
    assert endpoint.endpoint_type == "primary"
    assert endpoint.ip_policy == "dhcp_reserved"
    assert endpoint.dnsmasq_record_type == "host_record"
    assert endpoint.node_slug == "edge-1"

    ip_range = snapshot.ip_ranges[0]
    assert ip_range.range_policy == "dhcp_dynamic_pool"
    assert ip_range.lifecycle == "active"

    opconf = snapshot.operational_configs[0]
    assert opconf.actual_state_policy == "required"
    assert opconf.expected_host_os == "linux"
    assert opconf.power_control == "wol"
    assert opconf.local_endpoint is not None
    assert opconf.local_endpoint.node_slug == "edge-1"
    assert opconf.tailscale_endpoint is None

    placement = snapshot.placements[0]
    assert placement.desired_state == "active"
    assert placement.assignment_source == "manual"
    assert placement.config == {"enable_dhcp": True}
    assert placement.endpoint_id is None

    service = snapshot.services[0]
    assert service.service_type == "service"
    assert service.lifecycle == "active"

    dependency = snapshot.dependencies[0]
    assert dependency.resolution_status == "unresolved"
    assert dependency.resolved_service_id is None


def test_query_requests_all_desired_collections():
    for field in (
        "desired_nodes",
        "desired_endpoints",
        "desired_ip_ranges",
        "desired_node_operational_configs",
        "desired_service_placements",
        "desired_services",
        "desired_dependencies",
    ):
        assert field in DESIRED_QUERY
