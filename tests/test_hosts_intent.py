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
from nctl_core.sources.desired import DesiredEndpoint, DesiredNode


def node(
    name: str,
    slug: str,
    *,
    lifecycle: str = "planned",
    node_type: str = "device",
) -> DesiredNode:
    return DesiredNode(
        id=f"node-{slug}",
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
) -> DesiredEndpoint:
    return DesiredEndpoint(
        id=f"endpoint-{node_slug}-{name}",
        name=name,
        endpoint_type=endpoint_type,
        node_id=f"node-{node_slug}",
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
    assert "# schema_version: 3.0\n" in rendered
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

    assert payload["schema_version"] == "3.0"
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
