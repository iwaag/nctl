"""Ported from nintent's tests/test_ansible_inventory.py so the exported
vocabulary (skip reasons, groups, hostvars) survives the Phase 1.5 move."""

from __future__ import annotations

import yaml

from nctl_core.hosts_intent import (
    export_hosts_intent,
    hosts_intent_payload,
    render_hosts_intent_json,
    render_hosts_intent_yml,
)
from nctl_core.sources.desired import DesiredEndpoint, DesiredNode, DesiredServicePlacement
from nctl_core.ssh_trust import derive_host_key_alias

NODE_A_ID = "11111111-1111-1111-1111-111111111111"
NODE_B_ID = "22222222-2222-2222-2222-222222222222"
KNOWN_HOSTS_PATH = "/home/user/.local/state/nctl/ssh/known_hosts"


def node(
    name: str,
    slug: str,
    *,
    lifecycle: str = "planned",
    node_type: str = "device",
    id: str | None = None,
) -> DesiredNode:
    return DesiredNode(
        id=id or f"node-{slug}",
        name=name,
        slug=slug,
        lifecycle=lifecycle,
        node_type=node_type,
    )


def endpoint(
    name: str,
    node_slug: str,
    *,
    endpoint_type: str = "primary",
    mdns_name: str | None = None,
    node_id: str | None = None,
) -> DesiredEndpoint:
    return DesiredEndpoint(
        id=f"endpoint-{node_slug}-{name}",
        name=name,
        endpoint_type=endpoint_type,
        node_id=node_id or f"node-{node_slug}",
        node_slug=node_slug,
        mdns_name=mdns_name,
    )


def test_primary_endpoint_with_mdns_exports_ssh_host() -> None:
    export = export_hosts_intent(
        [node("ag Nomad", "agnomad")],
        [endpoint("primary", "agnomad", mdns_name="agnomad.local")],
    )

    assert export.summary["exported_hosts"] == 1
    assert export.summary["groups"] == ["ssh_hosts"]
    assert export.skipped == []
    ssh_hosts = export.inventory["all"]["children"]["ssh_hosts"]["hosts"]
    assert ssh_hosts["agnomad"]["ansible_host"] == "agnomad.local"
    assert ssh_hosts["agnomad"]["mdns_hostname"] == "agnomad.local"
    assert ssh_hosts["agnomad"]["nintent_inventory_stage"] == "reserved_name"
    assert ssh_hosts["agnomad"]["name_reserved_only"] is True
    assert "host_os" not in ssh_hosts["agnomad"]


def test_bootstrap_export_contains_no_service_groups() -> None:
    export = export_hosts_intent(
        [node("ag Nomad", "agnomad")],
        [endpoint("primary", "agnomad", mdns_name="agnomad.local")],
    )

    assert list(export.inventory["all"]["children"].keys()) == ["ssh_hosts"]


def test_service_host_exports_for_bootstrap_discovery() -> None:
    export = export_hosts_intent(
        [node("DNS service host", "agdns", node_type="service_host")],
        [endpoint("primary", "agdns", mdns_name="agdns.local")],
    )

    assert export.summary["exported_hosts"] == 1
    assert export.skipped == []
    assert "agdns" in export.inventory["all"]["children"]["ssh_hosts"]["hosts"]


def test_endpoint_selection_prefers_primary_then_management_then_fallback() -> None:
    nodes = [node("Management Only", "management-only"), node("Fallback", "fallback")]
    endpoints = [
        endpoint("svc", "management-only", endpoint_type="service", mdns_name="svc.local"),
        endpoint("mgmt", "management-only", endpoint_type="management", mdns_name="mgmt.local"),
        endpoint("zeta", "fallback", endpoint_type="vpn", mdns_name="zeta.local"),
        endpoint("alpha", "fallback", endpoint_type="service", mdns_name="alpha.local"),
    ]

    export = export_hosts_intent(nodes, endpoints)
    hosts = {host["inventory_hostname"]: host for host in export.hosts}

    assert hosts["management-only"]["desired_endpoint"] == "mgmt"
    assert hosts["fallback"]["desired_endpoint"] == "alpha"


def test_endpoint_list_order_does_not_affect_output() -> None:
    nodes = [node("Fallback", "fallback")]
    endpoints = [
        endpoint("zeta", "fallback", endpoint_type="vpn", mdns_name="zeta.local"),
        endpoint("alpha", "fallback", endpoint_type="service", mdns_name="alpha.local"),
    ]

    forward = export_hosts_intent(nodes, endpoints)
    reverse = export_hosts_intent(nodes, list(reversed(endpoints)))

    assert forward.as_dict() == reverse.as_dict()


def test_node_without_mdns_endpoint_is_skipped() -> None:
    export = export_hosts_intent(
        [node("No mDNS", "no-mdns")],
        [endpoint("primary", "no-mdns")],
    )

    assert export.summary["exported_hosts"] == 0
    assert export.summary["skipped_nodes"] == 1
    assert export.skipped[0]["reasons"] == ["missing_mdns_name"]


def test_ineligible_lifecycle_and_node_type_are_skipped() -> None:
    export = export_hosts_intent(
        [
            node("Retired", "retired", lifecycle="retired"),
            node("Container", "container", node_type="container"),
        ],
        [
            endpoint("primary", "retired", mdns_name="retired.local"),
            endpoint("primary", "container", mdns_name="container.local"),
        ],
    )
    reasons = {entry["desired_node_slug"]: entry["reasons"] for entry in export.skipped}

    assert reasons["retired"] == ["node_lifecycle_not_exportable"]
    assert reasons["container"] == ["node_type_not_exportable"]


def test_include_skipped_false_omits_details_but_counts() -> None:
    export = export_hosts_intent(
        [node("No mDNS", "no-mdns")],
        [],
        include_skipped=False,
    )

    assert export.summary["skipped_nodes"] == 1
    assert export.summary["skipped_details"] == 0
    assert export.skipped == []


def test_rendered_yaml_is_parseable_inventory() -> None:
    export = export_hosts_intent(
        [node("ag Nomad", "agnomad")],
        [endpoint("primary", "agnomad", mdns_name="agnomad.local")],
    )

    rendered = render_hosts_intent_yml(export, generated_at="2026-07-16T00:00:00+00:00")
    loaded = yaml.safe_load(rendered)

    assert "# Generated by nctl\n" in rendered
    assert "# schema_version: 5.0\n" in rendered
    assert "job_result_id" not in rendered
    assert loaded["all"]["children"]["ssh_hosts"]["hosts"]["agnomad"]["mdns_hostname"] == "agnomad.local"


def test_rendered_yaml_contains_no_host_os() -> None:
    export = export_hosts_intent(
        [node("ag Nomad", "agnomad")],
        [endpoint("primary", "agnomad", mdns_name="agnomad.local")],
    )

    rendered = render_hosts_intent_yml(export, generated_at="2026-07-16T00:00:00+00:00")

    assert "host_os" not in rendered


def test_json_payload_contains_inventory_hosts_and_skipped() -> None:
    export = export_hosts_intent(
        [node("ag Node", "agnode")],
        [endpoint("primary", "agnode", mdns_name="agnode.local")],
    )

    payload = hosts_intent_payload(export, generated_at="2026-07-16T00:00:00+00:00")

    assert payload["schema_version"] == "5.0"
    assert "job_result_id" not in payload
    assert payload["hosts"][0]["inventory_hostname"] == "agnode"
    assert "ssh_hosts" in payload["inventory"]["all"]["children"]
    assert render_hosts_intent_json(export, generated_at="2026-07-16T00:00:00+00:00").endswith("\n")


def test_summary_has_no_skipped_groups_field() -> None:
    export = export_hosts_intent(
        [node("ag Node", "agnode")],
        [endpoint("primary", "agnode", mdns_name="agnode.local")],
    )

    assert "skipped_groups" not in export.summary


def placement(
    node_id: str,
    *,
    deployment_profile: str,
    desired_state: str = "active",
    instance_name: str = "dnsmasq",
) -> DesiredServicePlacement:
    return DesiredServicePlacement(
        id=f"placement-{node_id}-{instance_name}",
        service_id="service-dnsmasq",
        node_id=node_id,
        instance_name=instance_name,
        desired_state=desired_state,
        deployment_profile=deployment_profile,
        config_schema_version="1",
    )


def test_active_placement_adds_bare_service_group() -> None:
    export = export_hosts_intent(
        [node("ag Dnsmasq", "agdnsmasq")],
        [endpoint("primary", "agdnsmasq", mdns_name="agdnsmasq.local")],
        placements=[placement("node-agdnsmasq", deployment_profile="dnsmasq")],
        profile_groups={"dnsmasq": "dnsmasq_server"},
    )

    assert export.summary["groups"] == ["dnsmasq_server", "ssh_hosts"]
    assert export.inventory["all"]["children"]["dnsmasq_server"]["hosts"] == {"agdnsmasq": {}}
    assert export.skipped == []


def test_inactive_placement_is_ignored() -> None:
    export = export_hosts_intent(
        [node("ag Dnsmasq", "agdnsmasq")],
        [endpoint("primary", "agdnsmasq", mdns_name="agdnsmasq.local")],
        placements=[placement("node-agdnsmasq", deployment_profile="dnsmasq", desired_state="staged")],
        profile_groups={"dnsmasq": "dnsmasq_server"},
    )

    assert export.summary["groups"] == ["ssh_hosts"]


def test_placement_with_unknown_profile_is_reported_not_dropped() -> None:
    export = export_hosts_intent(
        [node("ag Dnsmasq", "agdnsmasq")],
        [endpoint("primary", "agdnsmasq", mdns_name="agdnsmasq.local")],
        placements=[placement("node-agdnsmasq", deployment_profile="unknown_profile")],
        profile_groups={"dnsmasq": "dnsmasq_server"},
    )

    assert export.summary["groups"] == ["ssh_hosts"]
    assert len(export.skipped) == 1
    entry = export.skipped[0]
    assert entry["item_type"] == "desired_service_placement"
    assert entry["group"] == "unknown_profile"
    assert entry["reasons"] == ["unknown_deployment_profile"]


def test_placement_on_skipped_node_is_reported_not_dropped() -> None:
    export = export_hosts_intent(
        [node("No mDNS", "no-mdns")],
        [],
        placements=[placement("node-no-mdns", deployment_profile="dnsmasq")],
        profile_groups={"dnsmasq": "dnsmasq_server"},
    )

    assert export.summary["groups"] == []
    placement_skips = [entry for entry in export.skipped if entry["item_type"] == "desired_service_placement"]
    assert len(placement_skips) == 1
    assert placement_skips[0]["reasons"] == ["node_not_exported"]


def test_ssh_host_vars_absent_without_known_hosts_file() -> None:
    export = export_hosts_intent(
        [node("ag Nomad", "agnomad", id=NODE_A_ID)],
        [endpoint("primary", "agnomad", mdns_name="agnomad.local", node_id=NODE_A_ID)],
    )
    host_vars = export.inventory["all"]["children"]["ssh_hosts"]["hosts"]["agnomad"]
    assert "nctl_ssh_host_key_alias" not in host_vars
    assert "ansible_ssh_common_args" not in host_vars


def test_ssh_host_vars_derive_from_node_id_and_survive_mdns_rename() -> None:
    export_before = export_hosts_intent(
        [node("ag Nomad", "agnomad", id=NODE_A_ID)],
        [endpoint("primary", "agnomad", mdns_name="agnomad.local", node_id=NODE_A_ID)],
        ssh_known_hosts_file=KNOWN_HOSTS_PATH,
    )
    export_after = export_hosts_intent(
        [node("ag Nomad", "agnomad", id=NODE_A_ID)],
        [endpoint("primary", "agnomad", mdns_name="agnomad.home.arpa", node_id=NODE_A_ID)],
        ssh_known_hosts_file=KNOWN_HOSTS_PATH,
    )
    alias_before = export_before.inventory["all"]["children"]["ssh_hosts"]["hosts"]["agnomad"][
        "nctl_ssh_host_key_alias"
    ]
    alias_after = export_after.inventory["all"]["children"]["ssh_hosts"]["hosts"]["agnomad"][
        "nctl_ssh_host_key_alias"
    ]
    assert alias_before == alias_after == derive_host_key_alias(NODE_A_ID)


def test_ssh_host_vars_different_node_ids_get_different_aliases() -> None:
    export = export_hosts_intent(
        [node("Node A", "node-a", id=NODE_A_ID), node("Node B", "node-b", id=NODE_B_ID)],
        [
            endpoint("primary", "node-a", mdns_name="node-a.local", node_id=NODE_A_ID),
            endpoint("primary", "node-b", mdns_name="node-b.local", node_id=NODE_B_ID),
        ],
        ssh_known_hosts_file=KNOWN_HOSTS_PATH,
    )
    hosts = export.inventory["all"]["children"]["ssh_hosts"]["hosts"]
    alias_a = hosts["node-a"]["nctl_ssh_host_key_alias"]
    alias_b = hosts["node-b"]["nctl_ssh_host_key_alias"]
    assert alias_a != alias_b
    assert alias_a == derive_host_key_alias(NODE_A_ID)
    assert alias_b == derive_host_key_alias(NODE_B_ID)


def test_ssh_host_vars_carry_the_configured_path_and_strict_options() -> None:
    export = export_hosts_intent(
        [node("ag Nomad", "agnomad", id=NODE_A_ID)],
        [endpoint("primary", "agnomad", mdns_name="agnomad.local", node_id=NODE_A_ID)],
        ssh_known_hosts_file=KNOWN_HOSTS_PATH,
    )
    host_vars = export.inventory["all"]["children"]["ssh_hosts"]["hosts"]["agnomad"]
    args = host_vars["ansible_ssh_common_args"]
    assert f"HostKeyAlias={derive_host_key_alias(NODE_A_ID)}" in args
    assert f"UserKnownHostsFile={KNOWN_HOSTS_PATH}" in args
    assert "StrictHostKeyChecking=yes" in args
    assert "CheckHostIP=no" in args
    assert "UpdateHostKeys=no" in args
