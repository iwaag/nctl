"""Tier A/B coverage for `desired export` (state_bundle Step 1).

Distinct failure modes owned here:
- an incomplete or wrong document silently accepted as a backup (exact
  expected-document assertion over all ten writable kinds);
- nondeterministic bytes breaking snapshot diffing (byte-identical and
  input-order-independent assertions);
- silent data loss (unresolved reference, unexportable snapshot field, and
  decode-time source issue each fail the export by name).
"""

from __future__ import annotations

import yaml

from nctl_core.compute.model import DesiredComputeInstance, DesiredComputePlatform, DesiredSourceIssue
from nctl_core.desired_export import KIND_ORDER, document_counts, document_to_yaml, export_document
from nctl_core.sources.desired import (
    DesiredEndpoint,
    DesiredEndpointRef,
    DesiredIPRange,
    DesiredNode,
    DesiredNodeOperationalOverride,
    DesiredService,
    DesiredServiceBinding,
    DesiredServicePlacement,
    DesiredSnapshot,
    DesiredWorkspace,
)


def full_snapshot() -> DesiredSnapshot:
    return DesiredSnapshot(
        nodes=[
            DesiredNode(
                id="node-1", slug="agpc", name="agpc", lifecycle="active", node_type="device",
                role="workstation", accepted_actual_types=["device"],
                expected_spec={"cpu": {"cores": 8}, "arch": "amd64"}, realized_device_id="dev-1",
            ),
            DesiredNode(id="node-2", slug="aglxc01", name="aglxc01", lifecycle="active", node_type="service_host"),
        ],
        endpoints=[
            DesiredEndpoint(
                id="ep-1", name="primary", endpoint_type="primary", node_id="node-1", node_slug="agpc",
                ip_address="192.168.0.10/24", gateway_address="192.168.0.1", ip_policy="static",
                mac_address="02:00:00:00:00:01", mdns_name="agpc.local", generate_dnsmasq=True,
                dnsmasq_record_type="host_record", realized_ip_address_id="ip-1",
            ),
            DesiredEndpoint(
                id="ep-2", name="ollama-api", endpoint_type="service", node_id="node-1", node_slug="agpc",
                dns_name="agpc.home.arpa", protocol="http", port=11434,
            ),
        ],
        ip_ranges=[
            DesiredIPRange(
                id="range-1", name="dhcp", slug="dhcp", start_address="192.168.0.100",
                end_address="192.168.0.199", range_policy="dhcp", lifecycle="active",
                generate_dnsmasq=True, dnsmasq_options={"lease_time": "12h"},
            ),
        ],
        operational_overrides=[
            DesiredNodeOperationalOverride(
                id="ov-1", node_id="node-1", connection_path="local_then_tailscale", ansible_port=22,
                power_control="wol", is_laptop=False,
                local_endpoint=DesiredEndpointRef(
                    id="ep-1", name="primary", endpoint_type="primary", node_slug="agpc",
                ),
            ),
        ],
        placements=[
            DesiredServicePlacement(
                id="pl-1", service_id="svc-1", node_id="node-1", endpoint_id="ep-2",
                instance_name="ollama", desired_state="active", deployment_profile="ollama",
                config_schema_version="1", config={"b": 2, "a": 1},
            ),
            DesiredServicePlacement(
                id="pl-2", service_id="svc-2", node_id="node-2",
                instance_name="node-agent", deployment_profile="node_agent", config_schema_version="1",
            ),
        ],
        services=[
            DesiredService(id="svc-1", slug="ollama", name="ollama", lifecycle="active"),
            DesiredService(id="svc-2", slug="node-agent", name="node-agent", lifecycle="active"),
        ],
        service_bindings=[
            DesiredServiceBinding(
                id="b-1", binding_name="llm_provider", consumer_placement_id="pl-2",
                provider_service_id="svc-1", provider_service_slug="ollama",
            ),
        ],
        workspaces=[
            DesiredWorkspace(
                id="ws-1", slug="pj-clusterintent", name="pj-clusterintent", lifecycle="active",
                source_remote_url="https://github.com/iwaag/pj-clusterintent.git",
                expected_path="~/projects/pj-clusterintent", desired_presence="present",
                node_id="node-1", node_slug="agpc",
            ),
        ],
        compute_platforms=[
            DesiredComputePlatform(
                id="cp-1", name="pve-main", slug="pve-main", lifecycle="active",
                control_node_id="node-1", config={"api": {"verify_tls": False}}, realized_cluster_id="cluster-1",
            ),
        ],
        compute_instances=[
            DesiredComputeInstance(
                id="ci-1", desired_node_id="node-2", platform_id="cp-1", instance_kind="container",
                desired_power_state="running", desired_presence="present",
                vcpus=2, memory_mb=2048, root_disk_gb=16,
                config={"vmid": 101, "bridge": "vmbr0"}, realized_vm_id="vm-1",
            ),
        ],
    )


def test_exports_every_kind_with_exact_expected_operations():
    document, errors = export_document(full_snapshot())
    assert errors == []
    assert document["dry_run"] is True
    by_kind = {}
    for operation in document["operations"]:
        assert operation["op"] == "upsert"
        by_kind.setdefault(operation["kind"], []).append(operation)
    assert set(by_kind) == set(KIND_ORDER)

    node = next(op for op in by_kind["desired_node"] if op["key"] == {"slug": "agpc"})
    assert node["values"] == {
        "name": "agpc", "slug": "agpc", "node_type": "device", "lifecycle": "active",
        "role": "workstation", "accepted_actual_types": ["device"],
        "expected_spec": {"arch": "amd64", "cpu": {"cores": 8}}, "realized_device": "dev-1",
    }

    endpoint = next(op for op in by_kind["desired_endpoint"] if op["key"]["name"] == "primary")
    assert list(endpoint["key"]) == ["desired_node", "name", "endpoint_type"]
    assert endpoint["values"] == {
        "desired_node": "agpc", "name": "primary", "endpoint_type": "primary",
        "ip_address": "192.168.0.10/24", "gateway_address": "192.168.0.1", "ip_policy": "static",
        "mac_address": "02:00:00:00:00:01", "dns_name": None, "mdns_name": "agpc.local",
        "vpn_dns_name": None, "protocol": None, "port": None, "generate_dnsmasq": True,
        "dnsmasq_record_type": "host_record", "realized_ip_address": "ip-1",
    }

    platform = by_kind["desired_compute_platform"][0]
    assert platform["values"]["control_node"] == "agpc"
    assert platform["values"]["realized_cluster"] == "cluster-1"

    instance = by_kind["desired_compute_instance"][0]
    assert instance["key"] == {"desired_node": "aglxc01"}
    assert instance["values"]["platform"] == "pve-main"
    assert instance["values"]["realized_vm"] == "vm-1"

    placement = next(op for op in by_kind["desired_service_placement"] if op["key"]["instance_name"] == "ollama")
    assert placement["key"] == {"desired_service": "ollama", "instance_name": "ollama"}
    assert placement["values"]["desired_endpoint"] == {
        "desired_node": "agpc", "name": "ollama-api", "endpoint_type": "service",
    }
    assert placement["values"]["config"] == {"a": 1, "b": 2}

    binding = by_kind["desired_service_binding"][0]
    assert binding["key"] == {
        "consumer_placement": {"desired_service": "node-agent", "instance_name": "node-agent"},
        "binding_name": "llm_provider",
    }
    assert binding["values"]["provider_service"] == "ollama"

    override = by_kind["desired_node_operational_override"][0]
    assert override["key"] == {"desired_node": "agpc"}
    assert override["values"]["local_endpoint"] == {
        "desired_node": "agpc", "name": "primary", "endpoint_type": "primary",
    }
    assert override["values"]["tailscale_endpoint"] is None

    workspace = by_kind["desired_workspace"][0]
    assert workspace["values"]["desired_node"] == "agpc"

    assert document_counts(document) == {
        "desired_node": 2, "desired_ip_range": 1, "desired_endpoint": 2,
        "desired_compute_platform": 1, "desired_compute_instance": 1, "desired_service": 2,
        "desired_service_placement": 2, "desired_service_binding": 1,
        "desired_node_operational_override": 1, "desired_workspace": 1,
    }


def test_operations_are_sorted_by_writer_kind_order_then_identity():
    document, _errors = export_document(full_snapshot())
    kinds = [operation["kind"] for operation in document["operations"]]
    assert kinds == sorted(kinds, key=KIND_ORDER.index)
    node_slugs = [op["key"]["slug"] for op in document["operations"] if op["kind"] == "desired_node"]
    assert node_slugs == sorted(node_slugs)


def test_unchanged_state_yields_byte_identical_yaml_regardless_of_input_order():
    first = full_snapshot()
    second = full_snapshot()
    second.nodes.reverse()
    second.endpoints.reverse()
    second.placements.reverse()
    second.services.reverse()
    doc_first, _ = export_document(first)
    doc_second, _ = export_document(second)
    assert document_to_yaml(doc_first) == document_to_yaml(doc_second)
    assert document_to_yaml(doc_first) == document_to_yaml(export_document(full_snapshot())[0])


def test_yaml_round_trips_to_the_same_document():
    document, _errors = export_document(full_snapshot())
    assert yaml.safe_load(document_to_yaml(document)) == document


def test_unresolved_reference_fails_by_name():
    snapshot = full_snapshot()
    snapshot.placements[0].node_id = "node-missing"
    _document, errors = export_document(snapshot)
    assert any(
        error.code == "unresolved_reference"
        and error.detail["kind"] == "desired_service_placement"
        and error.detail["reference_id"] == "node-missing"
        for error in errors
    )


def test_source_issue_fails_export_by_name():
    snapshot = full_snapshot()
    snapshot.source_issues.append(DesiredSourceIssue(
        code="invalid_mac_address", target_kind="endpoint", target_id="ep-1",
        target_slug_or_name="primary", message="not a MAC",
    ))
    _document, errors = export_document(snapshot)
    assert any(
        error.code == "desired_source_issue" and "invalid_mac_address" in error.message
        for error in errors
    )


def test_readiness_excluded_instance_is_still_exported_and_not_fatal():
    snapshot = full_snapshot()
    unready = DesiredComputeInstance(
        id="ci-2", desired_node_id="node-1", platform_id="cp-1", instance_kind="container",
        vcpus=1, memory_mb=512, root_disk_gb=8, config={"vmid": 102},
    )
    snapshot.unready_compute_instances.append(unready)
    snapshot.source_issues.append(DesiredSourceIssue(
        code="compute_primary_endpoint_missing", target_kind="compute_instance",
        target_id="ci-2", message="node 'agpc' has no primary endpoint satisfying the compute NIC contract",
    ))
    document, errors = export_document(snapshot)
    assert errors == []
    instance_keys = [
        op["key"] for op in document["operations"] if op["kind"] == "desired_compute_instance"
    ]
    assert {"desired_node": "agpc"} in instance_keys and {"desired_node": "aglxc01"} in instance_keys


def test_duplicate_mac_issue_is_not_fatal():
    snapshot = full_snapshot()
    snapshot.source_issues.append(DesiredSourceIssue(
        code="duplicate_mac_address", target_kind="endpoint", target_id="ep-1",
        target_slug_or_name="primary", message="MAC address already used", scope="global",
    ))
    _document, errors = export_document(snapshot)
    assert errors == []


def test_unknown_snapshot_field_fails_instead_of_silent_drop():
    class WiderNode(DesiredNode):
        brand_new_field: str = "x"

    snapshot = full_snapshot()
    snapshot.nodes.append(WiderNode(id="node-3", slug="agnew", name="agnew", lifecycle="active", node_type="device"))
    _document, errors = export_document(snapshot)
    assert any(
        error.code == "unexportable_field" and error.detail == {"kind": "desired_node", "fields": ["brand_new_field"]}
        for error in errors
    )
