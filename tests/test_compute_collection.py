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
        "desired_dependencies": [],
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
def test_malformed_compute_rows_are_isolated_from_healthy_snapshot():
    """A bad platform, a bad instance, and a bad MAC each become their own
    issue; none of them prevent an unrelated healthy node/endpoint from
    parsing normally in the same call (plan.md Section 5.9 isolation)."""
    healthy_node = _healthy_node(node_id="node-1", slug="healthy")
    good_endpoint = {
        "id": "endpoint-good",
        "name": "primary",
        "endpoint_type": "PRIMARY",
        "ip_address": "192.0.2.20/32",
        "ip_policy": "static",
        "dns_name": None,
        "dns_name_source": None,
        "mdns_name": "healthy.local",
        "mdns_name_source": None,
        "vpn_dns_name": None,
        "mac_address": "aa:bb:cc:dd:ee:01",
        "protocol": None,
        "port": None,
        "generate_dnsmasq": False,
        "dnsmasq_record_type": "HOST_RECORD",
        "realized_ip_address": None,
        "realized_ip_address_source": None,
        "desired_node": {"id": "node-1", "slug": "healthy"},
    }
    bad_mac_endpoint = {**good_endpoint, "id": "endpoint-bad-mac", "mac_address": "not-a-mac"}
    bad_platform = {
        "id": "platform-bad",
        "name": "bad-platform",
        "slug": "bad-platform",
        "provider_type": "VMWARE",
        "lifecycle": "ACTIVE",
        "control_node": {"id": "node-1", "slug": "healthy"},
        "config_schema_version": "v1",
        "config": {},
        "realized_cluster": None,
        "realized_cluster_source": None,
    }
    bad_instance = {
        "id": "instance-bad",
        "desired_node": {"id": "node-1", "slug": "healthy"},
        "platform": {"id": "platform-bad", "slug": "bad-platform"},
        "instance_kind": "GPU_PASSTHROUGH",
        "desired_power_state": "RUNNING",
        "vcpus": 2,
        "memory_mb": 2048,
        "root_disk_gb": 20,
        "config_schema_version": "v1",
        "config": {},
        "realized_vm": None,
        "realized_vm_source": None,
    }

    client = _mock_graphql(
        _base_response(
            desired_nodes=[healthy_node],
            desired_endpoints=[good_endpoint, bad_mac_endpoint],
            desired_compute_platforms=[bad_platform],
            desired_compute_instances=[bad_instance],
        )
    )

    snapshot = fetch_desired_snapshot(client)

    # The healthy node/endpoint parse exactly as if the bad rows weren't there.
    assert [n.slug for n in snapshot.nodes] == ["healthy"]
    assert {e.id for e in snapshot.endpoints} == {"endpoint-good", "endpoint-bad-mac"}
    good = next(e for e in snapshot.endpoints if e.id == "endpoint-good")
    assert good.mac_address == "aa:bb:cc:dd:ee:01"
    bad = next(e for e in snapshot.endpoints if e.id == "endpoint-bad-mac")
    assert bad.mac_address is None  # malformed MAC never crashes endpoint parsing

    # Provider/schema discriminators are code constants, so only the invalid
    # instance is excluded from the typed collections.
    assert [platform.id for platform in snapshot.compute_platforms] == ["platform-bad"]
    assert snapshot.compute_instances == []

    codes_by_target = {issue.target_id: issue.code for issue in snapshot.source_issues}
    assert codes_by_target == {
        "instance-bad": "invalid_instance_kind",
        "endpoint-bad-mac": "invalid_mac_address",
    }
    assert {issue.target_kind for issue in snapshot.source_issues} == {
        "compute_instance",
        "endpoint",
    }


@respx.mock
def test_duplicate_platform_slug_excludes_both_with_global_issue():
    node = _healthy_node()
    platform_a = _healthy_platform(platform_id="platform-a", slug="dup")
    platform_b = _healthy_platform(platform_id="platform-b", slug="dup")

    client = _mock_graphql(
        _base_response(desired_nodes=[node], desired_compute_platforms=[platform_a, platform_b])
    )

    snapshot = fetch_desired_snapshot(client)

    assert snapshot.compute_platforms == []
    issues = [issue for issue in snapshot.source_issues if issue.code == "duplicate_platform_slug"]
    assert {issue.target_id for issue in issues} == {"platform-a", "platform-b"}
    assert all(issue.scope == "global" for issue in issues)


@respx.mock
def test_duplicate_compute_instance_for_node_keeps_first_flags_rest():
    node = _healthy_node(node_id="node-1", slug="healthy")
    platform = _healthy_platform()  # lifecycle=planned -> not actionable, no endpoint needed
    instance_a = _healthy_instance(instance_id="instance-a", node_id="node-1")
    instance_b = _healthy_instance(instance_id="instance-b", node_id="node-1")

    client = _mock_graphql(
        _base_response(
            desired_nodes=[node],
            desired_compute_platforms=[platform],
            desired_compute_instances=[instance_a, instance_b],
        )
    )

    snapshot = fetch_desired_snapshot(client)

    assert [i.id for i in snapshot.compute_instances] == ["instance-a"]
    duplicate_issues = [issue for issue in snapshot.source_issues if issue.code == "duplicate_compute_instance_for_node"]
    assert [issue.target_id for issue in duplicate_issues] == ["instance-b"]
    assert duplicate_issues[0].scope == "target"


@respx.mock
def test_dangling_instance_references_produce_target_scoped_issues():
    node = _healthy_node(node_id="node-1", slug="healthy")
    platform = _healthy_platform()
    missing_platform_instance = _healthy_instance(
        instance_id="instance-orphan-platform", node_id="node-1", platform_id="platform-ghost"
    )
    missing_node_instance = _healthy_instance(
        instance_id="instance-orphan-node", node_id="node-ghost", platform_id="platform-1"
    )

    client = _mock_graphql(
        _base_response(
            desired_nodes=[node],
            desired_compute_platforms=[platform],
            desired_compute_instances=[missing_platform_instance, missing_node_instance],
        )
    )

    snapshot = fetch_desired_snapshot(client)

    assert snapshot.compute_instances == []
    codes_by_target = {issue.target_id: issue.code for issue in snapshot.source_issues}
    assert codes_by_target["instance-orphan-platform"] == "compute_instance_platform_missing"
    assert codes_by_target["instance-orphan-node"] == "compute_instance_node_missing"


@respx.mock
def test_invalid_platform_blocks_dependent_instance_and_records_blocked_consumers():
    node = _healthy_node(node_id="node-1", slug="healthy")
    orphan_platform = {
        "id": "platform-orphan",
        "name": "orphan",
        "slug": "orphan",
        "provider_type": "PROXMOX",
        "lifecycle": "ACTIVE",
        "control_node": {"id": "node-ghost", "slug": "ghost"},  # dangling -> invalid platform
        "config_schema_version": "v1",
        "config": {},
        "realized_cluster": None,
        "realized_cluster_source": None,
    }
    dependent_instance = _healthy_instance(
        instance_id="instance-orphaned", node_id="node-1", platform_id="platform-orphan"
    )

    client = _mock_graphql(
        _base_response(
            desired_nodes=[node],
            desired_compute_platforms=[orphan_platform],
            desired_compute_instances=[dependent_instance],
        )
    )

    snapshot = fetch_desired_snapshot(client)

    assert snapshot.compute_platforms == []
    assert snapshot.compute_instances == []

    platform_issue = next(issue for issue in snapshot.source_issues if issue.target_id == "platform-orphan")
    assert platform_issue.code == "compute_platform_control_node_missing"
    assert platform_issue.blocked_consumers == ["instance-orphaned"]

    instance_issue = next(issue for issue in snapshot.source_issues if issue.target_id == "instance-orphaned")
    assert instance_issue.code == "compute_instance_platform_invalid"
    assert instance_issue.scope == "target"


@respx.mock
def test_active_effective_instance_without_primary_endpoint_is_blocked():
    node = _healthy_node(node_id="node-1", slug="healthy")
    node = {**node, "lifecycle": "ACTIVE"}
    platform = _healthy_platform()
    platform = {**platform, "lifecycle": "ACTIVE"}
    instance = _healthy_instance(instance_id="instance-1", node_id="node-1")

    client = _mock_graphql(
        _base_response(
            desired_nodes=[node],
            desired_compute_platforms=[platform],
            desired_compute_instances=[instance],
            # no endpoints at all for node-1
        )
    )

    snapshot = fetch_desired_snapshot(client)

    assert snapshot.compute_instances == []
    issue = next(issue for issue in snapshot.source_issues if issue.target_id == "instance-1")
    assert issue.code == "compute_primary_endpoint_missing"


@respx.mock
def test_planned_effective_instance_is_exempt_from_endpoint_completeness():
    """A non-actionable planned draft may have zero/incomplete endpoints (plan.md Section 5.4/5.5)."""
    node = _healthy_node(node_id="node-1", slug="healthy")  # lifecycle=approved by default
    platform = _healthy_platform()  # lifecycle=planned
    instance = _healthy_instance(instance_id="instance-1", node_id="node-1")

    client = _mock_graphql(
        _base_response(
            desired_nodes=[node],
            desired_compute_platforms=[platform],
            desired_compute_instances=[instance],
        )
    )

    snapshot = fetch_desired_snapshot(client)

    assert snapshot.source_issues == []
    assert [i.id for i in snapshot.compute_instances] == ["instance-1"]


def test_effective_lifecycle_all_branches():
    assert effective_lifecycle("retired", "active") == "retired"
    assert effective_lifecycle("active", "retired") == "retired"
    assert effective_lifecycle("deprecated", "active") == "deprecated"
    assert effective_lifecycle("active", "deprecated") == "deprecated"
    assert effective_lifecycle("planned", "active") == "planned"
    assert effective_lifecycle("active", "planned") == "planned"
    assert effective_lifecycle("active", "active") == "active"
    assert effective_lifecycle("approved", "active") == "approved"
    assert effective_lifecycle("active", "approved") == "approved"
    assert effective_lifecycle("approved", "approved") == "approved"
    # retired beats every other value, including deprecated/planned in the same pair.
    assert effective_lifecycle("retired", "deprecated") == "retired"
    assert effective_lifecycle("deprecated", "planned") == "deprecated"


def _endpoint(**overrides) -> DesiredEndpoint:
    base = dict(
        id="endpoint-1", name="primary", endpoint_type="primary", node_id="node-1", node_slug="agvm1",
        mac_address="aa:bb:cc:dd:ee:ff", mdns_name="agvm1.local", ip_policy="static", ip_address="192.0.2.5/24", gateway_address="192.0.2.1",
    )
    base.update(overrides)
    return DesiredEndpoint(**base)


def test_select_compute_primary_endpoint_zero_candidates():
    endpoint, code = select_compute_primary_endpoint([])
    assert endpoint is None
    assert code == "compute_primary_endpoint_missing"


def test_select_compute_primary_endpoint_ambiguous_candidates():
    endpoints = [_endpoint(id="endpoint-a"), _endpoint(id="endpoint-b")]
    endpoint, code = select_compute_primary_endpoint(endpoints)
    assert endpoint is None
    assert code == "compute_primary_endpoint_ambiguous"


def test_select_compute_primary_endpoint_single_valid_candidate():
    valid = _endpoint(id="endpoint-good")
    not_primary = _endpoint(id="endpoint-mgmt", endpoint_type="management")
    no_mac = _endpoint(id="endpoint-no-mac", mac_address=None)
    endpoint, code = select_compute_primary_endpoint([valid, not_primary, no_mac])
    assert code is None
    assert endpoint is not None
    assert endpoint.id == "endpoint-good"
