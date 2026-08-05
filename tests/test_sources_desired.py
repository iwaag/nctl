from __future__ import annotations

import httpx
import respx

from nctl_core.nautobot import NautobotClient
from nctl_core.compute.contract import effective_lifecycle, select_compute_primary_endpoint
from nctl_core.sources.desired import (
    DESIRED_QUERY,
    DesiredEndpoint,
    DesiredNode,
    fetch_desired_snapshot,
)

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


def _base_response(**overrides) -> dict:
    """A minimal-but-complete GraphQL response for `fetch_desired_snapshot`.

    Every root is present with an empty list unless overridden, matching the
    real Nautobot response shape.
    """
    body = {
        "desired_nodes": [],
        "desired_endpoints": [],
        "desired_ip_ranges": [],
        "desired_node_operational_overrides": [],
        "desired_service_placements": [],
        "desired_services": [],
        "desired_service_bindings": [],
        "desired_compute_platforms": [],
        "desired_compute_instances": [],
    }
    body.update(overrides)
    return {"data": body}


def _mock_graphql(body: dict) -> NautobotClient:
    respx.post(f"{BASE_URL}/api/graphql/").mock(return_value=httpx.Response(200, json=body))
    return NautobotClient(BASE_URL, "tok")


def _healthy_node(node_id: str = "node-1", slug: str = "edge-1") -> dict:
    return {
        "id": node_id,
        "slug": slug,
        "name": slug,
        "lifecycle": "APPROVED",
        "node_type": "DEVICE",
        "role": None,
        "accepted_actual_types": ["DEVICE"],
        "expected_spec": {},
        "realized_device": None,
        "realized_device_source": None,
    }


def _healthy_platform(platform_id: str = "platform-1", slug: str = "aghub-pve", control_node_id: str = "node-1") -> dict:
    return {
        "id": platform_id,
        "name": "aghub-pve",
        "slug": slug,
        "provider_type": "PROXMOX",
        "lifecycle": "PLANNED",
        "control_node": {"id": control_node_id, "slug": "edge-1"},
        "config_schema_version": "v1",
        "config": {"cluster_name": "aghub", "default_storage": "local-lvm", "default_bridge": "vmbr0"},
        "realized_cluster": None,
        "realized_cluster_source": None,
    }


def _healthy_instance(
    instance_id: str = "instance-1", node_id: str = "node-2", platform_id: str = "platform-1"
) -> dict:
    return {
        "id": instance_id,
        "desired_node": {"id": node_id, "slug": "agvm1"},
        "platform": {"id": platform_id, "slug": "aghub-pve"},
        "instance_kind": "CONTAINER",
        "desired_power_state": "RUNNING",
        "vcpus": 2,
        "memory_mb": 2048,
        "root_disk_gb": 20,
        "config_schema_version": "v1",
        "config": {"template": "local:vztmpl/debian-12.tar.zst", "unprivileged": True},
        "realized_vm": None,
        "realized_vm_source": None,
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
                            "accepted_actual_types": ["DEVICE"],
                            "expected_spec": {"serial": "SER123"},
                            "realized_device": {"id": "dev-1"},
                            "realized_device_source": "DERIVED",
                        },
                        {
                            "id": "node-2",
                            "slug": "agvm1",
                            "name": "agvm1",
                            "lifecycle": "PLANNED",
                            "node_type": "SERVICE_HOST",
                            "role": None,
                            "accepted_actual_types": [],
                            "expected_spec": {},
                            "realized_device": None,
                            "realized_device_source": None,
                        },
                    ],
                    "desired_endpoints": [
                        {
                            "id": "endpoint-1",
                            "name": "primary",
                            "endpoint_type": "PRIMARY",
                            "ip_address": "192.0.2.10/32",
                            "ip_policy": "DHCP_RESERVED",
                            "dns_name": "edge-1.example.test",
                            "dns_name_source": "INTENT",
                            "mdns_name": "edge-1.local",
                            "mdns_name_source": "DERIVED",
                            "vpn_dns_name": None,
                            "mac_address": "AA:BB:CC:DD:EE:FF",
                            "protocol": None,
                            "port": None,
                            "generate_dnsmasq": True,
                            "dnsmasq_record_type": "HOST_RECORD",
                            "realized_ip_address": {"id": "ip-1"},
                            "realized_ip_address_source": "OVERRIDE",
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
                    "desired_node_operational_overrides": [
                        {
                            "id": "override-1",
                            "desired_node": {"id": "node-1"},
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
                            "lifecycle": "ACTIVE",
                        }
                    ],
                    "desired_service_bindings": [
                        {
                            "id": "binding-1",
                            "binding_name": "llm_provider",
                            "consumer_placement": {"id": "placement-1"},
                            "provider_service": {"id": "service-1", "slug": "dnsmasq-service"},
                        }
                    ],
                    "desired_workspaces": [
                        {
                            "id": "workspace-1",
                            "slug": "pj-voxel3dprint",
                            "name": "pj-voxel3dprint",
                            "lifecycle": "ACTIVE",
                            "source_remote_url": "https://github.com/iwaag/pj-voxel3dprint.git",
                            "expected_path": "/home/eiji/projects/pj-voxel3dprint",
                            "desired_presence": "PRESENT",
                            "desired_node": {"id": "node-1", "slug": "edge-1"},
                        }
                    ],
                    "desired_compute_platforms": [_healthy_platform()],
                    "desired_compute_instances": [_healthy_instance()],
                }
            },
        )
    )
    client = NautobotClient(BASE_URL, "tok")

    snapshot = fetch_desired_snapshot(client)

    node = snapshot.nodes[0]
    assert node.lifecycle == "approved"
    assert node.node_type == "device"
    assert node.accepted_actual_types == ["device"]
    assert node.expected_spec == {"serial": "SER123"}
    assert node.realized_device_id == "dev-1"
    assert not hasattr(node, "realized_vm_id")

    endpoint = snapshot.endpoints[0]
    assert endpoint.endpoint_type == "primary"
    assert endpoint.ip_policy == "dhcp_reserved"
    assert endpoint.dnsmasq_record_type == "host_record"
    assert endpoint.node_slug == "edge-1"
    assert endpoint.realized_ip_address_id == "ip-1"
    assert endpoint.mac_address == "aa:bb:cc:dd:ee:ff"

    assert snapshot.source_issues == []
    platform = snapshot.compute_platforms[0]
    assert platform.slug == "aghub-pve"
    assert platform.lifecycle == "planned"
    assert platform.control_node_id == "node-1"
    assert platform.config == {"cluster_name": "aghub", "default_storage": "local-lvm", "default_bridge": "vmbr0"}
    assert platform.realized_cluster_id is None

    instance = snapshot.compute_instances[0]
    assert instance.desired_node_id == "node-2"
    assert instance.platform_id == "platform-1"
    assert instance.instance_kind == "container"
    assert instance.desired_power_state == "running"
    assert instance.desired_presence == "present"
    assert instance.config == {"template": "local:vztmpl/debian-12.tar.zst", "unprivileged": True}

    ip_range = snapshot.ip_ranges[0]
    assert ip_range.range_policy == "dhcp_dynamic_pool"
    assert ip_range.lifecycle == "active"

    override = snapshot.operational_overrides[0]
    assert override.power_control == "wol"
    assert override.local_endpoint is not None
    assert override.local_endpoint.node_slug == "edge-1"
    assert override.tailscale_endpoint is None

    placement = snapshot.placements[0]
    assert placement.desired_state == "active"
    assert placement.config == {"enable_dhcp": True}
    assert placement.endpoint_id is None

    service = snapshot.services[0]
    assert service.lifecycle == "active"

    binding = snapshot.service_bindings[0]
    assert binding.binding_name == "llm_provider"
    assert binding.consumer_placement_id == "placement-1"
    assert binding.provider_service_id == "service-1"
    assert binding.provider_service_slug == "dnsmasq-service"

    workspace = snapshot.workspaces[0]
    assert workspace.slug == "pj-voxel3dprint"
    assert workspace.lifecycle == "active"
    assert workspace.desired_presence == "present"
    assert workspace.source_remote_url == "https://github.com/iwaag/pj-voxel3dprint.git"
    assert workspace.expected_path == "/home/eiji/projects/pj-voxel3dprint"
    assert workspace.node_id == "node-1"
    assert workspace.node_slug == "edge-1"


@respx.mock
def test_fetch_desired_snapshot_retains_readiness_excluded_instance_as_unready():
    """A NIC-readiness exclusion must not lose the row: it moves to
    `unready_compute_instances` (desired-export fidelity) while planning
    input `compute_instances` and the source issue stay unchanged."""
    node = _healthy_node(node_id="node-2", slug="agvm1")
    node["lifecycle"] = "ACTIVE"
    platform = _healthy_platform(control_node_id="node-2")
    platform["lifecycle"] = "ACTIVE"
    client = _mock_graphql(_base_response(
        desired_nodes=[node],
        desired_compute_platforms=[platform],
        desired_compute_instances=[_healthy_instance()],
    ))

    snapshot = fetch_desired_snapshot(client)

    assert snapshot.compute_instances == []
    assert [issue.code for issue in snapshot.source_issues] == ["compute_primary_endpoint_missing"]
    assert len(snapshot.unready_compute_instances) == 1
    unready = snapshot.unready_compute_instances[0]
    assert unready.id == "instance-1"
    assert unready.desired_node_id == "node-2"
    assert unready.config == {"template": "local:vztmpl/debian-12.tar.zst", "unprivileged": True}


@respx.mock
def test_fetch_desired_snapshot_defaults_workspaces_when_field_absent():
    client = _mock_graphql(_base_response())

    snapshot = fetch_desired_snapshot(client)

    assert snapshot.workspaces == []


def test_query_requests_all_desired_collections():
    for field in (
        "desired_nodes",
        "desired_endpoints",
        "desired_ip_ranges",
        "desired_node_operational_overrides",
        "desired_service_placements",
        "desired_services",
        "desired_service_bindings",
        "desired_workspaces",
        "desired_compute_platforms",
        "desired_compute_instances",
    ):
        assert field in DESIRED_QUERY
    assert "mac_address" in DESIRED_QUERY
    # `DesiredNode.realized_vm(+_source)` was removed outright (VM p3 Step 5); the only
    # remaining `realized_vm` field is `DesiredComputeInstance.realized_vm`, inside the
    # `desired_compute_instances` block, not `desired_nodes`.
    nodes_block = DESIRED_QUERY.split("desired_endpoints {", 1)[0]
    assert "realized_vm" not in nodes_block
