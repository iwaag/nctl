from __future__ import annotations

import hashlib

from nctl_core.dnsmasq import (
    dnsmasq_content_sha256,
    dnsmasq_export_payload,
    export_dnsmasq_records,
    render_dnsmasq_export_json,
    render_dnsmasq_records_conf,
    resolve_dhcp_reservation,
)


def node(name: str, slug: str, lifecycle: str) -> dict:
    return {"id": f"node-{slug}", "name": name, "slug": slug, "lifecycle": lifecycle}


def endpoint(
    *,
    name: str,
    dns_name: str | None,
    ip_address: str | None,
    desired_node: dict,
    endpoint_type: str = "primary",
    generate_dnsmasq: bool = True,
    ip_policy: str = "dhcp_reserved",
    dnsmasq_record_type: str = "host_record",
    mdns_name: str | None = None,
    vpn_dns_name: str | None = None,
) -> dict:
    return {
        "id": f"endpoint-{name}",
        "name": name,
        "desired_node": desired_node,
        "endpoint_type": endpoint_type,
        "ip_address": ip_address,
        "dns_name": dns_name,
        "mdns_name": mdns_name,
        "vpn_dns_name": vpn_dns_name,
        "generate_dnsmasq": generate_dnsmasq,
        "ip_policy": ip_policy,
        "dnsmasq_record_type": dnsmasq_record_type,
    }


def endpoint_evaluation(endpoint_obj: dict, *, mac_candidates=None, ready: bool = True) -> dict:
    return {
        str(endpoint_obj["id"]): {
            "deterministic_summary": {"dhcp_reservation_ready": ready},
            "observed_facts": {"dhcp_mac_candidates": mac_candidates or []},
        }
    }


def ip_range(
    *,
    name: str = "home-dynamic-dhcp",
    slug: str = "home-dynamic-dhcp",
    start_address: str = "192.168.0.200",
    end_address: str = "192.168.0.250",
    range_policy: str = "dhcp_dynamic_pool",
    lifecycle: str = "active",
    generate_dnsmasq: bool = True,
    dnsmasq_options=None,
) -> dict:
    return {
        "id": f"range-{slug}",
        "name": name,
        "slug": slug,
        "start_address": start_address,
        "end_address": end_address,
        "range_policy": range_policy,
        "lifecycle": lifecycle,
        "generate_dnsmasq": generate_dnsmasq,
        "dnsmasq_options": dnsmasq_options or {},
    }


def mac_candidate(*, mac_address="AA-BB-CC-DD-EE-FF", node_name="Edge 1", node_id="actual-node-1", interface_name="eth0") -> dict:
    return {
        "actual_node_ref": {
            "object_type": "dcim.device",
            "id": node_id,
            "name": node_name,
        },
        "interface_id": f"interface-{interface_name}",
        "interface_name": interface_name,
        "mac_address": mac_address,
    }


def test_export_filters_eligible_endpoints_and_keeps_mdns_as_metadata() -> None:
    active = node("Edge 1", "edge-1", "active")
    retired = node("Old Edge", "old-edge", "retired")
    endpoints = [
        endpoint(name="mgmt", desired_node=active, dns_name="edge-1.example.test", ip_address="192.0.2.10/32", mdns_name="edge-1.local"),
        endpoint(name="off", desired_node=active, dns_name="off.example.test", ip_address="192.0.2.11", generate_dnsmasq=False),
        endpoint(name="mdns", desired_node=active, dns_name="mdns.example.test", ip_address="192.0.2.12", endpoint_type="mdns"),
        endpoint(name="old", desired_node=retired, dns_name="old.example.test", ip_address="192.0.2.13"),
        endpoint(name="nameless", desired_node=active, dns_name=None, ip_address="192.0.2.14"),
    ]

    export = export_dnsmasq_records(endpoints)

    assert export.summary["dns_records"] == 1
    assert export.summary["skipped"]["dns_records"] == 4
    assert export.dns_records[0]["line"] == "host-record=edge-1.example.test,192.0.2.10"
    assert export.dns_records[0]["mdns_name"] == "edge-1.local"
    skipped_reasons = {
        (entry["item_type"], entry["endpoint_name"]): entry["reasons"]
        for entry in export.skipped
        if entry["item_type"] == "dns_record"
    }
    assert skipped_reasons[("dns_record", "off")] == ["generate_dnsmasq_false"]
    assert skipped_reasons[("dns_record", "mdns")] == ["endpoint_type_not_exportable"]
    assert skipped_reasons[("dns_record", "old")] == ["node_lifecycle_not_exportable"]
    assert skipped_reasons[("dns_record", "nameless")] == ["missing_dns_name"]
    dhcp_skipped_reasons = {
        entry["endpoint_name"]: entry["reasons"]
        for entry in export.skipped
        if entry["item_type"] == "dhcp_reservation"
    }
    assert "node_lifecycle_not_exportable" in dhcp_skipped_reasons["old"]


def test_export_formats_record_types_and_sort_order() -> None:
    approved = node("App 1", "app-1", "approved")
    planned = node("VPN 1", "vpn-1", "planned")
    endpoints = [
        endpoint(
            name="vpn",
            desired_node=planned,
            endpoint_type="vpn",
            dns_name="vpn-target.example.test",
            ip_address="198.51.100.10/32",
            dnsmasq_record_type="cname",
            vpn_dns_name="vpn.example.test",
        ),
        endpoint(
            name="svc",
            desired_node=approved,
            endpoint_type="service",
            dns_name="api.example.test",
            ip_address="198.51.100.20/32",
            dnsmasq_record_type="address",
        ),
        endpoint(
            name="primary",
            desired_node=approved,
            dns_name="app.example.test",
            ip_address="198.51.100.30/32",
            dnsmasq_record_type="host_record",
        ),
    ]

    export = export_dnsmasq_records(endpoints, include_skipped=False)

    assert export.skipped == []
    assert export.summary["total_endpoints"] == 3
    assert export.summary["skipped"]["dns_records"] == 0
    assert export.summary["skipped_endpoint_details"] == 0
    assert [record["line"] for record in export.dns_records] == [
        "address=/api.example.test/198.51.100.20",
        "host-record=app.example.test,198.51.100.30",
        "cname=vpn.example.test,vpn-target.example.test",
    ]
    assert export.summary["record_types"] == {"address": 1, "cname": 1, "host_record": 1}


def test_cname_requires_vpn_dns_alias() -> None:
    active = node("VPN 2", "vpn-2", "active")
    export = export_dnsmasq_records(
        [
            endpoint(
                name="vpn",
                desired_node=active,
                endpoint_type="vpn",
                dns_name="vpn-target.example.test",
                ip_address="203.0.113.10",
                dnsmasq_record_type="cname",
            )
        ]
    )

    assert export.dns_records == []
    dns_skip = [entry for entry in export.skipped if entry["item_type"] == "dns_record"][0]
    assert dns_skip["reasons"] == ["missing_cname_alias"]


def test_dns_record_is_exported_without_mac_but_dhcp_is_skipped() -> None:
    active = node("Edge 1", "edge-1", "active")
    primary = endpoint(
        name="primary",
        desired_node=active,
        dns_name="edge-1.example.test",
        ip_address="192.0.2.10/32",
    )

    export = export_dnsmasq_records([primary])

    assert export.dns_records[0]["line"] == "host-record=edge-1.example.test,192.0.2.10"
    assert export.dhcp_reservations == []
    dhcp_skip = [entry for entry in export.skipped if entry["item_type"] == "dhcp_reservation"][0]
    assert "missing_endpoint_evaluation" in dhcp_skip["reasons"]
    assert "missing_actual_node" in dhcp_skip["reasons"]
    assert "missing_mac_address" in dhcp_skip["reasons"]


def test_static_endpoint_exports_dns_but_not_dhcp_reservation() -> None:
    active = node("Edge 1", "edge-1", "active")
    primary = endpoint(
        name="primary",
        desired_node=active,
        dns_name="edge-1.example.test",
        ip_address="192.0.2.10/32",
        ip_policy="static",
    )

    export = export_dnsmasq_records(
        [primary],
        endpoint_evaluations=endpoint_evaluation(primary, mac_candidates=[mac_candidate()]),
    )

    assert export.dns_records[0]["line"] == "host-record=edge-1.example.test,192.0.2.10"
    assert export.dhcp_reservations == []
    dhcp_skip = [entry for entry in export.skipped if entry["item_type"] == "dhcp_reservation"][0]
    assert dhcp_skip["reasons"] == ["ip_policy_not_dhcp_reserved"]


def test_dhcp_reservation_is_exported_when_mac_is_unique() -> None:
    active = node("Edge 1", "edge-1", "active")
    primary = endpoint(
        name="primary",
        desired_node=active,
        dns_name="edge-1.example.test",
        ip_address="192.0.2.10/32",
    )
    export = export_dnsmasq_records(
        [primary],
        endpoint_evaluations=endpoint_evaluation(primary, mac_candidates=[mac_candidate()]),
    )

    assert export.summary["dhcp_reservations"] == 1
    assert export.dhcp_reservations[0]["line"] == "dhcp-host=aa:bb:cc:dd:ee:ff,edge-1.example.test,192.0.2.10"


def test_dhcp_reservation_skips_ambiguous_or_invalid_mac() -> None:
    active = node("Edge 1", "edge-1", "active")
    primary = endpoint(
        name="primary",
        desired_node=active,
        dns_name="edge-1.example.test",
        ip_address="192.0.2.10/32",
    )

    ambiguous = resolve_dhcp_reservation(
        primary,
        endpoint_evaluation={
            "deterministic_summary": {"dhcp_reservation_ready": False},
            "observed_facts": {
                "dhcp_mac_candidates": [
                    mac_candidate(mac_address="aa:bb:cc:dd:ee:ff", interface_name="eth0"),
                    mac_candidate(mac_address="11:22:33:44:55:66", interface_name="eth1"),
                ]
            },
        },
    )
    invalid = resolve_dhcp_reservation(
        primary,
        endpoint_evaluation={
            "deterministic_summary": {"dhcp_reservation_ready": True},
            "observed_facts": {"dhcp_mac_candidates": [mac_candidate(mac_address="not-a-mac")]},
        },
    )

    assert "ambiguous_interface" in ambiguous["skip_reasons"]
    assert "invalid_mac_address" in invalid["skip_reasons"]


def test_render_outputs_for_ansible_consumption() -> None:
    active = node("Edge 1", "edge-1", "active")
    primary = endpoint(
        name="primary",
        desired_node=active,
        dns_name="edge-1.example.test",
        ip_address="192.0.2.10/32",
    )
    export = export_dnsmasq_records(
        [primary],
        endpoint_evaluations=endpoint_evaluation(primary, mac_candidates=[mac_candidate()]),
    )

    conf = render_dnsmasq_records_conf(export)
    assert conf == "\n".join(
        [
            "# Generated by nctl",
            "# schema_version: 5.0",
            "host-record=edge-1.example.test,192.0.2.10",
            "dhcp-host=aa:bb:cc:dd:ee:ff,edge-1.example.test,192.0.2.10",
            "",
        ]
    )

    payload = dnsmasq_export_payload(
        export,
        generated_at="2026-06-23T00:00:00+00:00",
        operation_id="op-123",
    )
    assert payload["schema_version"] == "5.0"
    assert payload["operation_id"] == "op-123"
    assert payload["dns_records"][0]["line"] == "host-record=edge-1.example.test,192.0.2.10"
    assert payload["dhcp_reservations"][0]["line"] == "dhcp-host=aa:bb:cc:dd:ee:ff,edge-1.example.test,192.0.2.10"
    assert payload["dhcp_ranges"] == []
    assert render_dnsmasq_export_json(export, generated_at="2026-06-23T00:00:00+00:00").endswith("\n")


def test_dynamic_desired_ranges_export_stable_dhcp_range_lines() -> None:
    active = node("Edge 1", "edge-1", "active")
    export = export_dnsmasq_records(
        [
            endpoint(
                name="primary",
                desired_node=active,
                dns_name="edge-1.example.test",
                ip_address="192.0.2.10/32",
                ip_policy="static",
            )
        ],
        ip_ranges=[
            ip_range(
                name="late",
                slug="late",
                start_address="192.168.0.220",
                end_address="192.168.0.250",
                dnsmasq_options={"lease_time": "6h"},
            ),
            ip_range(
                name="early",
                slug="early",
                start_address="192.168.0.200/24",
                end_address="192.168.0.210/24",
                dnsmasq_options={"lease_time": "12h"},
            ),
        ],
        include_skipped=False,
    )

    assert export.summary["dhcp_ranges"] == 2
    assert [entry["line"] for entry in export.dhcp_ranges] == [
        "dhcp-range=192.168.0.200,192.168.0.210,12h",
        "dhcp-range=192.168.0.220,192.168.0.250,6h",
    ]
    conf = render_dnsmasq_records_conf(export)
    assert "dhcp-range=192.168.0.200,192.168.0.210,12h\n" in conf


def test_desired_range_without_lease_time_omits_fourth_field() -> None:
    export = export_dnsmasq_records([], ip_ranges=[ip_range(dnsmasq_options={})])

    assert export.dhcp_ranges[0]["line"] == "dhcp-range=192.168.0.200,192.168.0.250"
    assert export.dhcp_ranges[0]["lease_time"] == ""


def test_non_dynamic_or_invalid_ranges_are_skipped() -> None:
    export = export_dnsmasq_records(
        [],
        ip_ranges=[
            ip_range(name="static", slug="static", range_policy="static_pool"),
            ip_range(name="off", slug="off", generate_dnsmasq=False),
            ip_range(name="bad", slug="bad", start_address="not-an-ip"),
            ip_range(name="old", slug="old", lifecycle="retired"),
        ],
    )

    assert export.dhcp_ranges == []
    assert export.summary["skipped_range_details"] == 4
    assert export.summary["skipped_ranges"] == 4
    skipped = {entry["slug"]: entry["reasons"] for entry in export.skipped if entry["item_type"] == "dhcp_range"}
    assert skipped["static"] == ["range_policy_not_dhcp_dynamic_pool"]
    assert skipped["off"] == ["generate_dnsmasq_false"]
    assert skipped["bad"] == ["invalid_start_address"]
    assert skipped["old"] == ["range_lifecycle_not_exportable"]


def test_json_payload_separates_dns_reservations_ranges_and_skipped() -> None:
    active = node("Edge 1", "edge-1", "active")
    primary = endpoint(
        name="primary",
        desired_node=active,
        dns_name="edge-1.example.test",
        ip_address="192.0.2.10/32",
    )
    export = export_dnsmasq_records(
        [primary],
        ip_ranges=[ip_range(dnsmasq_options={"lease_time": "12h"})],
        endpoint_evaluations=endpoint_evaluation(primary, mac_candidates=[mac_candidate()]),
    )
    payload = dnsmasq_export_payload(export, generated_at="2026-06-23T00:00:00+00:00")

    assert list(payload.keys()) == [
        "schema_version",
        "generated_at",
        "operation_id",
        "summary",
        "dns_records",
        "dhcp_reservations",
        "dhcp_ranges",
        "skipped",
    ]
    assert payload["dns_records"][0]["line"] == "host-record=edge-1.example.test,192.0.2.10"
    assert payload["dhcp_reservations"][0]["line"] == "dhcp-host=aa:bb:cc:dd:ee:ff,edge-1.example.test,192.0.2.10"
    assert payload["dhcp_ranges"][0]["line"] == "dhcp-range=192.168.0.200,192.168.0.250,12h"


def _one_endpoint_export():
    active = node("Edge 1", "edge-1", "active")
    primary = endpoint(
        name="primary", desired_node=active, dns_name="edge-1.example.test", ip_address="192.0.2.10/32",
    )
    return export_dnsmasq_records(
        [primary], endpoint_evaluations=endpoint_evaluation(primary, mac_candidates=[mac_candidate()]),
    )


def test_equal_source_state_at_different_times_and_operation_ids_renders_byte_identical_conf() -> None:
    export = _one_endpoint_export()
    conf_a = render_dnsmasq_records_conf(export)
    conf_b = render_dnsmasq_records_conf(export)
    assert conf_a == conf_b
    assert dnsmasq_content_sha256(conf_a) == dnsmasq_content_sha256(conf_b)
    # dnsmasq_export_payload's generated_at/operation_id never affect the conf bytes.
    payload_a = dnsmasq_export_payload(export, generated_at="2026-01-01T00:00:00+00:00", operation_id="op-a")
    payload_b = dnsmasq_export_payload(export, generated_at="2099-12-31T23:59:59+00:00", operation_id="op-b")
    assert payload_a["dns_records"] == payload_b["dns_records"]


def test_a_meaningful_directive_change_changes_the_digest() -> None:
    base_conf = render_dnsmasq_records_conf(_one_endpoint_export())
    base_digest = dnsmasq_content_sha256(base_conf)

    changed_node = node("Edge 1", "edge-1", "active")
    changed_endpoint = endpoint(
        name="primary", desired_node=changed_node, dns_name="edge-1.example.test", ip_address="192.0.2.99/32",
    )
    changed_export = export_dnsmasq_records(
        [changed_endpoint],
        endpoint_evaluations=endpoint_evaluation(changed_endpoint, mac_candidates=[mac_candidate()]),
    )
    changed_digest = dnsmasq_content_sha256(render_dnsmasq_records_conf(changed_export))

    assert changed_digest != base_digest


def test_a_new_dhcp_range_changes_the_digest() -> None:
    without_range = render_dnsmasq_records_conf(export_dnsmasq_records([]))
    with_range = render_dnsmasq_records_conf(export_dnsmasq_records([], ip_ranges=[ip_range()]))
    assert dnsmasq_content_sha256(without_range) != dnsmasq_content_sha256(with_range)


def test_deployed_conf_bytes_never_contain_runtime_metadata_comments() -> None:
    conf = render_dnsmasq_records_conf(_one_endpoint_export())
    assert "generated_at" not in conf
    assert "operation_id" not in conf
    assert conf.splitlines()[:2] == ["# Generated by nctl", "# schema_version: 5.0"]


def test_content_sha256_matches_a_standard_independent_sha256_implementation() -> None:
    conf = render_dnsmasq_records_conf(_one_endpoint_export())
    assert dnsmasq_content_sha256(conf) == hashlib.sha256(conf.encode("utf-8")).hexdigest()
    assert len(dnsmasq_content_sha256(conf)) == 64
    assert dnsmasq_content_sha256(conf) == dnsmasq_content_sha256(conf).lower()
