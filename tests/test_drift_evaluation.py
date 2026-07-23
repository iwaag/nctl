"""Pytest port of nintent's `nautobot_intent_catalog/tests/test_evaluations.py`
(Phase 2 Step 4). Fixtures are pydantic Step 1 read-models instead of
`SimpleNamespace` ORM stand-ins; realized objects and interfaces are passed
as explicit parameters (`realized_device=`, `interfaces_by_device_id=`)
instead of Django FK/relation traversal — see `drift/evaluation.py`'s module
docstring for why.
"""

from __future__ import annotations

from nctl_core.drift.evaluation import (
    classify_endpoint_ip_ranges,
    evaluate_endpoint_intent,
    evaluate_node_intent,
    evaluate_service_intent,
    invalid_desired_ip_ranges,
    matching_desired_ip_ranges,
    normalize_desired_range_addresses,
    normalize_endpoint_ip_string,
    overlapping_desired_ip_ranges,
)
from nctl_core.sources.actual import ActualDevice, ActualInterface, ActualIPAddress, ActualVirtualMachine
from nctl_core.sources.desired import DesiredDependency, DesiredEndpoint, DesiredIPRange, DesiredNode, DesiredService


def node(**overrides):
    data = dict(
        id="11111111-1111-1111-1111-111111111111",
        slug="edge-1",
        name="edge-1",
        node_type="device",
        lifecycle="active",
        role="edge",
        expected_spec={},
        accepted_actual_types=[],
    )
    data.update(overrides)
    return DesiredNode(**data)


def endpoint(**overrides):
    data = dict(
        id="22222222-2222-2222-2222-222222222222",
        name="primary",
        endpoint_type="primary",
        node_id="11111111-1111-1111-1111-111111111111",
        node_slug="edge-1",
        ip_address="192.0.2.10/32",
        ip_policy="dhcp_reserved",
        dns_name="edge-1.example.test",
        generate_dnsmasq=True,
        dnsmasq_record_type="host_record",
    )
    data.update(overrides)
    return DesiredEndpoint(**data)


def ip_range(**overrides):
    data = dict(
        id="44444444-4444-4444-4444-444444444444",
        name="home-dynamic-dhcp",
        slug="home-dynamic-dhcp",
        start_address="192.168.0.200",
        end_address="192.168.0.250",
        range_policy="dhcp_dynamic_pool",
        lifecycle="active",
        generate_dnsmasq=True,
    )
    data.update(overrides)
    return DesiredIPRange(**data)


def actual_device(**overrides):
    data = dict(
        id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        name="edge-1",
        serial="SER123",
        platform="ubuntu",
        facts={},
    )
    data.update(overrides)
    return ActualDevice(**data)


def actual_vm(**overrides):
    data = dict(id="vm-1", name="edge-1")
    data.update(overrides)
    return ActualVirtualMachine(**data)


def actual_ip(**overrides):
    data = dict(
        id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        host="192.0.2.10",
        mask_length=32,
        dns_name="edge-1.example.test",
    )
    data.update(overrides)
    return ActualIPAddress(**data)


def interface(**overrides):
    data = dict(
        id="cccccccc-cccc-cccc-cccc-cccccccccccc",
        name="eth0",
        mac_address="AA-BB-CC-DD-EE-FF",
        enabled=True,
        device_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    )
    data.update(overrides)
    return ActualInterface(**data)


def service(**overrides):
    data = dict(
        id="33333333-3333-3333-3333-333333333333",
        slug="api-service",
        name="api-service",
        display_name="API Service",
        service_type="service",
        lifecycle="active",
        catalog_namespace="default",
        catalog_metadata_name="api",
        requirements={"memory_gb": 2},
    )
    data.update(overrides)
    return DesiredService(**data)


def dependency(**overrides):
    data = dict(
        id="dep-1",
        source_service_id="33333333-3333-3333-3333-333333333333",
        dependency_kind="component",
        namespace="default",
        name="database",
        raw_ref="component:default/database",
        dependency_type="component",
        resolution_status="unresolved",
    )
    data.update(overrides)
    return DesiredDependency(**data)


# --- node evaluation --------------------------------------------------------


def test_explicit_realized_link_is_satisfied_and_skips_candidate_adoption():
    realized = actual_device(id="dev-real", name="edge-real", serial="SER123")
    desired = node(name="edge-1", accepted_actual_types=["device"], expected_spec={"serial": "SER123"}, realized_device_id=realized.id)
    conflicting_candidate = actual_device(id="dev-conflict", name="edge-1", serial="OTHER")

    payload = evaluate_node_intent(desired, device_candidates=[conflicting_candidate], realized_device=realized)

    assert payload.status == "satisfied"
    assert payload.actual_refs[0]["id"] == realized.id
    assert payload.observed_facts["candidates"] == []


def test_missing_node_records_missing_evaluation_instead_of_raising():
    payload = evaluate_node_intent(node(name="unknown", slug="unknown"))

    assert payload.status == "missing"
    assert payload.gap_summary["gaps"][0]["code"] == "missing_actual_node"
    assert payload.recommended_actions[0]["action"] == "link_desired_node_to_actual"


def test_unique_candidate_is_partial_and_requires_review():
    payload = evaluate_node_intent(
        node(name="edge-1", slug="edge-1"),
        device_candidates=[actual_device(name="edge-1")],
    )

    assert payload.status == "partial"
    assert payload.actual_refs[0]["object_type"] == "dcim.device"
    assert payload.gap_summary["gaps"][0]["code"] == "actual_node_not_linked"
    assert payload.recommended_actions[0]["requires_review"] is True


def test_device_only_node_does_not_adopt_vm_candidate():
    payload = evaluate_node_intent(
        node(name="edge-1", slug="edge-1", accepted_actual_types=["device"]),
        vm_candidates=[actual_vm(name="edge-1")],
    )

    assert payload.status == "missing"
    assert payload.actual_refs == []
    assert payload.observed_facts["candidates"] == []
    assert payload.expected_facts["accepted_actual_types"] == ["device"]


def test_virtual_machine_only_node_does_not_adopt_device_candidate():
    payload = evaluate_node_intent(
        node(name="edge-1", slug="edge-1", node_type="virtual_machine", accepted_actual_types=["virtual_machine"]),
        device_candidates=[actual_device(name="edge-1")],
    )

    assert payload.status == "missing"
    assert payload.actual_refs == []
    assert payload.observed_facts["candidates"] == []


def test_multiple_accepted_actual_types_allow_device_and_vm_candidates():
    payload = evaluate_node_intent(
        node(name="edge-1", slug="edge-1", node_type="service_host", accepted_actual_types=["device", "virtual_machine"]),
        device_candidates=[actual_device(id="dev-a", name="edge-1")],
        vm_candidates=[actual_vm(id="vm-a", name="edge-1")],
    )

    assert payload.status == "conflict"
    assert {c["actual_ref"]["object_type"] for c in payload.observed_facts["candidates"]} == {
        "dcim.device",
        "virtualization.virtualmachine",
    }


def test_realized_link_outside_accepted_actual_types_is_conflict():
    realized = actual_device(name="edge-1")
    payload = evaluate_node_intent(
        node(node_type="virtual_machine", accepted_actual_types=["virtual_machine"], realized_device_id=realized.id),
        realized_device=realized,
    )

    assert payload.status == "conflict"
    assert payload.gap_summary["gaps"][0]["code"] == "realized_actual_type_not_accepted"


def test_name_normalized_candidate_is_partial_and_requires_review():
    payload = evaluate_node_intent(
        node(name="pc1", slug="pc1"),
        device_candidates=[actual_device(name="pc1.local")],
    )

    assert payload.status == "partial"
    assert payload.actual_refs[0]["name"] == "pc1.local"
    assert payload.observed_facts["candidates"][0]["match_reasons"] == ["name_or_hostname"]


def test_explicit_link_mismatch_is_conflict():
    realized = actual_device(serial="ACTUAL")
    payload = evaluate_node_intent(
        node(realized_device_id=realized.id, expected_spec={"serial": "EXPECTED"}),
        realized_device=realized,
    )

    assert payload.status == "conflict"
    assert payload.gap_summary["gaps"][0]["code"] == "serial_mismatch"


def test_name_normalized_explicit_hostname_link_is_not_conflict():
    realized = actual_device(name="pc1.local")
    payload = evaluate_node_intent(
        node(name="pc1", realized_device_id=realized.id, expected_spec={"hostname": "pc1"}),
        realized_device=realized,
    )

    assert payload.status == "satisfied"
    assert payload.gap_summary["gaps"] == []


def test_unrelated_fqdn_candidate_is_not_collapsed_to_short_name():
    payload = evaluate_node_intent(
        node(name="db01", slug="db01"),
        device_candidates=[actual_device(name="db01.prod.example.com")],
    )

    assert payload.status == "missing"
    assert payload.observed_facts["candidates"] == []


def test_ambiguous_candidates_are_conflict():
    desired = node(expected_spec={"serial": "SER123"})
    payload = evaluate_node_intent(
        desired,
        device_candidates=[
            actual_device(id="dev-a", name="node-a", serial="SER123"),
            actual_device(id="dev-b", name="node-b", serial="SER123"),
        ],
    )

    assert payload.status == "conflict"
    assert payload.gap_summary["gaps"][0]["code"] == "ambiguous_actual_node_candidates"


# --- IP range classification -------------------------------------------------


def test_endpoint_ip_and_range_addresses_are_normalized_to_hosts():
    desired_range = ip_range(start_address="192.168.0.200/24", end_address="192.168.0.250/24")

    assert normalize_endpoint_ip_string("192.168.0.210/24") == "192.168.0.210"
    assert normalize_desired_range_addresses(desired_range) == {
        "start_address": "192.168.0.200",
        "end_address": "192.168.0.250",
        "valid": True,
        "errors": [],
    }


def test_ipv4_endpoint_range_matches_are_deterministically_sorted():
    broad = ip_range(
        id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        name="broad",
        slug="broad",
        start_address="192.168.0.1",
        end_address="192.168.0.254",
        range_policy="static_pool",
    )
    narrow = ip_range(
        id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        name="reserved",
        slug="reserved",
        start_address="192.168.0.100",
        end_address="192.168.0.199",
        range_policy="dhcp_reservable_pool",
    )

    matches = matching_desired_ip_ranges("192.168.0.120/24", [narrow, broad])

    assert [match["slug"] for match in matches] == ["broad", "reserved"]
    assert matches[0]["start_address"] == "192.168.0.1"
    assert matches[1]["range_policy"] == "dhcp_reservable_pool"


def test_invalid_endpoint_ip_does_not_raise():
    classification = classify_endpoint_ip_ranges("not-an-ip", [ip_range()])

    assert classification["endpoint_ip_valid"] is False
    assert classification["endpoint_ip"] == "not-an-ip"
    assert classification["matching_ranges"] == []
    assert classification["invalid_ranges"] == []


def test_invalid_range_definitions_are_reported():
    invalids = invalid_desired_ip_ranges(
        [
            ip_range(name="bad-start", slug="bad-start", start_address="not-an-ip"),
            ip_range(name="reversed", slug="reversed", start_address="192.168.0.250", end_address="192.168.0.200"),
            ip_range(name="mixed", slug="mixed", start_address="192.168.0.1", end_address="2001:db8::1"),
        ]
    )

    errors_by_slug = {entry["slug"]: entry["errors"] for entry in invalids}
    assert errors_by_slug["bad-start"] == ["invalid_start_address"]
    assert errors_by_slug["reversed"] == ["range_start_after_end"]
    assert errors_by_slug["mixed"] == ["address_family_mismatch"]


def test_overlapping_matching_ranges_are_detected():
    first = ip_range(
        id="11111111-1111-1111-1111-111111111111",
        name="reservable",
        slug="reservable",
        start_address="192.168.0.100",
        end_address="192.168.0.180",
        range_policy="dhcp_reservable_pool",
    )
    second = ip_range(
        id="22222222-2222-2222-2222-222222222222",
        name="dynamic",
        slug="dynamic",
        start_address="192.168.0.150",
        end_address="192.168.0.220",
        range_policy="dhcp_dynamic_pool",
    )
    third = ip_range(
        id="33333333-3333-3333-3333-333333333333",
        name="other",
        slug="other",
        start_address="192.168.1.10",
        end_address="192.168.1.20",
    )

    classification = classify_endpoint_ip_ranges("192.168.0.160", [third, second, first])
    overlaps = overlapping_desired_ip_ranges([third, second, first])

    assert [match["slug"] for match in classification["matching_ranges"]] == ["reservable", "dynamic"]
    assert len(classification["overlapping_matching_ranges"]) == 1
    assert classification["overlapping_matching_ranges"][0]["overlap_start_address"] == "192.168.0.150"
    assert classification["overlapping_matching_ranges"][0]["overlap_end_address"] == "192.168.0.180"
    assert len(overlaps) == 1


# --- endpoint evaluation ------------------------------------------------------


def test_realized_ip_and_single_mac_candidate_are_satisfied():
    dev = actual_device()
    desired_node = node(realized_device_id=dev.id)
    ifaces = {dev.id: [interface(device_id=dev.id)]}

    payload = evaluate_endpoint_intent(
        endpoint(node_id=desired_node.id),
        desired_node=desired_node,
        realized_ip=actual_ip(),
        node_realized_device=dev,
        interfaces_by_device_id=ifaces,
    )

    assert payload.status == "satisfied"
    assert payload.deterministic_summary["dhcp_reservation_ready"] is True
    assert payload.observed_facts["dhcp_mac_candidates"][0]["mac_address"] == "aa:bb:cc:dd:ee:ff"


def test_ip_mismatch_is_conflict():
    payload = evaluate_endpoint_intent(
        endpoint(),
        realized_ip=actual_ip(host="192.0.2.20", mask_length=32),
    )

    assert payload.status == "conflict"
    assert payload.gap_summary["gaps"][0]["code"] == "ip_address_mismatch"


def test_ip_candidates_match_host_and_mask_length():
    matching_ip = actual_ip()
    payload = evaluate_endpoint_intent(
        endpoint(generate_dnsmasq=False),
        ip_candidates=[matching_ip],
    )

    assert payload.status == "partial"
    assert payload.actual_refs[0]["name"] == "192.0.2.10/32"
    assert payload.observed_facts["ip_candidates"][0]["facts"]["address"] == "192.0.2.10/32"
    assert payload.gap_summary["gaps"][0]["code"] == "actual_ip_address_not_linked"


def test_missing_ip_and_missing_mac_are_partial():
    dev = actual_device()
    desired_node = node(realized_device_id=dev.id)
    ifaces = {dev.id: [interface(device_id=dev.id, mac_address=None)]}

    payload = evaluate_endpoint_intent(
        endpoint(node_id=desired_node.id),
        desired_node=desired_node,
        node_realized_device=dev,
        interfaces_by_device_id=ifaces,
        ip_candidates=[],
    )

    assert payload.status == "partial"
    gap_codes = [gap["code"] for gap in payload.gap_summary["gaps"]]
    assert "missing_actual_ip_address" in gap_codes
    assert "missing_mac_address" in gap_codes
    assert payload.deterministic_summary["dhcp_reservation_ready"] is False


def test_dhcp_reserved_endpoint_missing_ip_needs_no_observation():
    payload = evaluate_endpoint_intent(endpoint(ip_policy="dhcp_reserved"), ip_candidates=[])

    gap_codes = [gap["code"] for gap in payload.gap_summary["gaps"]]
    assert "missing_actual_ip_address" in gap_codes
    assert not any(code.startswith("ipam_reconcile_observation_") for code in gap_codes)


def test_static_endpoint_without_observation_is_manual_review_gap():
    dev = actual_device(facts={})
    desired_node = node(realized_device_id=dev.id)

    payload = evaluate_endpoint_intent(
        endpoint(node_id=desired_node.id, ip_policy="static"),
        desired_node=desired_node,
        node_realized_device=dev,
        ip_candidates=[],
    )

    gap_codes = [gap["code"] for gap in payload.gap_summary["gaps"]]
    assert gap_codes == ["ipam_reconcile_observation_missing"]
    assert "missing_actual_ip_address" not in gap_codes


def test_static_endpoint_with_matching_observation_is_automatic_gap():
    dev = actual_device(facts={"primary_ip_address": "192.0.2.10", "last_seen": "2026-07-23T00:00:00Z"})
    desired_node = node(realized_device_id=dev.id)

    payload = evaluate_endpoint_intent(
        endpoint(node_id=desired_node.id, ip_policy="static"),
        desired_node=desired_node,
        node_realized_device=dev,
        ip_candidates=[],
    )

    assert payload.gap_summary["gaps"][0]["code"] == "missing_actual_ip_address"
    gap = payload.gap_summary["gaps"][0]
    assert gap["expected"]["endpoint_id"] == "22222222-2222-2222-2222-222222222222"
    assert gap["expected"]["ip_policy"] == "static"


def test_external_endpoint_with_mismatched_observation_is_manual_review_gap():
    dev = actual_device(facts={"primary_ip_address": "198.51.100.5"})
    desired_node = node(realized_device_id=dev.id)

    payload = evaluate_endpoint_intent(
        endpoint(node_id=desired_node.id, ip_policy="external"),
        desired_node=desired_node,
        node_realized_device=dev,
        ip_candidates=[],
    )

    gap_codes = [gap["code"] for gap in payload.gap_summary["gaps"]]
    assert gap_codes == ["ipam_reconcile_observation_mismatch"]


def test_static_endpoint_observation_matches_by_host_portion():
    dev = actual_device(facts={"primary_ip_address": "192.0.2.10/24"})
    desired_node = node(realized_device_id=dev.id)

    payload = evaluate_endpoint_intent(
        endpoint(node_id=desired_node.id, ip_policy="static", ip_address="192.0.2.10/32"),
        desired_node=desired_node,
        node_realized_device=dev,
        ip_candidates=[],
    )

    gap_codes = [gap["code"] for gap in payload.gap_summary["gaps"]]
    assert "missing_actual_ip_address" in gap_codes
    assert not any(code.startswith("ipam_reconcile_observation_") for code in gap_codes)


def test_static_endpoint_without_realized_device_is_observation_missing():
    payload = evaluate_endpoint_intent(endpoint(ip_policy="static"), ip_candidates=[])

    gap_codes = [gap["code"] for gap in payload.gap_summary["gaps"]]
    assert gap_codes == ["ipam_reconcile_observation_missing"]


def test_multiple_mac_candidates_are_not_dhcp_ready():
    dev = actual_device()
    desired_node = node(realized_device_id=dev.id)
    ifaces = {
        dev.id: [
            interface(id="iface-0", device_id=dev.id, name="eth0", mac_address="aa:bb:cc:dd:ee:ff"),
            interface(id="iface-1", device_id=dev.id, name="eth1", mac_address="11:22:33:44:55:66"),
        ]
    }

    payload = evaluate_endpoint_intent(
        endpoint(node_id=desired_node.id),
        desired_node=desired_node,
        realized_ip=actual_ip(),
        node_realized_device=dev,
        interfaces_by_device_id=ifaces,
    )

    assert payload.status == "partial"
    assert payload.gap_summary["gaps"][0]["code"] == "ambiguous_interface"
    assert payload.deterministic_summary["dhcp_reservation_ready"] is False
    assert payload.recommended_actions[0]["action"] == "select_dhcp_interface"


def test_node_evaluation_candidate_interfaces_supply_mac_candidates():
    desired_node = node(name="pc1", slug="pc1")
    candidate = actual_device(id="dev-candidate", name="pc1.local")
    ifaces = {candidate.id: [interface(device_id=candidate.id, name="eth0", mac_address="aa-bb-cc-dd-ee-ff")]}
    node_payload = evaluate_node_intent(desired_node, device_candidates=[candidate], interfaces_by_device_id=ifaces)

    payload = evaluate_endpoint_intent(
        endpoint(node_id=desired_node.id),
        desired_node=desired_node,
        realized_ip=actual_ip(),
        node_evaluation=node_payload,
        interfaces_by_device_id=ifaces,
    )

    assert payload.status == "satisfied"
    assert payload.deterministic_summary["dhcp_reservation_ready"] is True
    assert payload.observed_facts["dhcp_mac_candidates"][0]["mac_address"] == "aa:bb:cc:dd:ee:ff"
    assert payload.observed_facts["dhcp_mac_candidates"][0]["actual_node_ref"]["name"] == "pc1.local"


def test_realized_device_primary_mac_custom_field_supplies_mac_candidate():
    dev = actual_device(facts={"primary_mac_address": "AA-BB-CC-DD-EE-FF"})
    desired_node = node(realized_device_id=dev.id)

    payload = evaluate_endpoint_intent(
        endpoint(node_id=desired_node.id),
        desired_node=desired_node,
        realized_ip=actual_ip(),
        node_realized_device=dev,
    )

    assert payload.status == "satisfied"
    assert payload.deterministic_summary["dhcp_reservation_ready"] is True
    assert payload.observed_facts["dhcp_mac_candidates"][0]["mac_address"] == "aa:bb:cc:dd:ee:ff"
    assert payload.observed_facts["dhcp_mac_candidates"][0]["interface_name"] == "primary_mac_address"


def test_dhcp_reserved_endpoint_in_reservable_pool_can_be_dhcp_ready():
    dev = actual_device()
    desired_node = node(realized_device_id=dev.id)
    ifaces = {dev.id: [interface(device_id=dev.id)]}
    desired_range = ip_range(
        name="reservable", slug="reservable", start_address="192.0.2.1", end_address="192.0.2.200", range_policy="dhcp_reservable_pool"
    )

    payload = evaluate_endpoint_intent(
        endpoint(node_id=desired_node.id),
        desired_node=desired_node,
        realized_ip=actual_ip(),
        node_realized_device=dev,
        interfaces_by_device_id=ifaces,
        range_candidates=[desired_range],
    )

    assert payload.status == "satisfied"
    assert payload.expected_facts["ip_policy"] == "dhcp_reserved"
    assert payload.deterministic_summary["dhcp_reservation_ready"] is True
    assert payload.observed_facts["matching_ip_policy_ranges"][0]["slug"] == "reservable"


def test_dhcp_reserved_endpoint_in_dynamic_pool_is_partial_and_not_ready():
    dev = actual_device()
    desired_node = node(realized_device_id=dev.id)
    ifaces = {dev.id: [interface(device_id=dev.id)]}
    desired_range = ip_range(
        name="dynamic", slug="dynamic", start_address="192.0.2.1", end_address="192.0.2.200", range_policy="dhcp_dynamic_pool"
    )

    payload = evaluate_endpoint_intent(
        endpoint(node_id=desired_node.id),
        desired_node=desired_node,
        realized_ip=actual_ip(),
        node_realized_device=dev,
        interfaces_by_device_id=ifaces,
        range_candidates=[desired_range],
    )

    assert payload.status == "partial"
    assert payload.deterministic_summary["dhcp_reservation_ready"] is False
    assert "dhcp_reserved_endpoint_in_dynamic_pool" in payload.deterministic_summary["gap_codes"]


def test_static_endpoint_with_mac_is_not_dhcp_ready():
    dev = actual_device()
    desired_node = node(realized_device_id=dev.id)
    ifaces = {dev.id: [interface(device_id=dev.id)]}
    desired_range = ip_range(name="static", slug="static", start_address="192.0.2.1", end_address="192.0.2.200", range_policy="static_pool")

    payload = evaluate_endpoint_intent(
        endpoint(node_id=desired_node.id, ip_policy="static"),
        desired_node=desired_node,
        realized_ip=actual_ip(),
        node_realized_device=dev,
        interfaces_by_device_id=ifaces,
        range_candidates=[desired_range],
    )

    assert payload.status == "satisfied"
    assert payload.deterministic_summary["dhcp_reservation_ready"] is False


def test_missing_and_ambiguous_policy_ranges_are_reported():
    dev = actual_device()
    desired_node = node(realized_device_id=dev.id)
    ifaces = {dev.id: [interface(device_id=dev.id)]}
    first = ip_range(
        name="reservable", slug="reservable", start_address="192.0.2.1", end_address="192.0.2.200", range_policy="dhcp_reservable_pool"
    )
    second = ip_range(name="static", slug="static", start_address="192.0.2.10", end_address="192.0.2.20", range_policy="static_pool")

    kwargs = dict(
        desired_node=desired_node,
        realized_ip=actual_ip(),
        node_realized_device=dev,
        interfaces_by_device_id=ifaces,
    )
    missing = evaluate_endpoint_intent(endpoint(node_id=desired_node.id), range_candidates=[], **kwargs)
    ambiguous = evaluate_endpoint_intent(endpoint(node_id=desired_node.id), range_candidates=[first, second], **kwargs)

    assert "missing_ip_policy_range" in missing.deterministic_summary["gap_codes"]
    assert missing.deterministic_summary["dhcp_reservation_ready"] is False
    assert "ambiguous_ip_policy_range" in ambiguous.deterministic_summary["gap_codes"]
    assert ambiguous.deterministic_summary["dhcp_reservation_ready"] is False


def test_invalid_policy_range_is_reported_without_crashing():
    dev = actual_device()
    desired_node = node(realized_device_id=dev.id)
    ifaces = {dev.id: [interface(device_id=dev.id)]}
    invalid_range = ip_range(start_address="not-an-ip")

    payload = evaluate_endpoint_intent(
        endpoint(node_id=desired_node.id),
        desired_node=desired_node,
        realized_ip=actual_ip(),
        node_realized_device=dev,
        interfaces_by_device_id=ifaces,
        range_candidates=[invalid_range],
    )

    assert payload.status == "partial"
    assert "invalid_ip_policy_range" in payload.deterministic_summary["gap_codes"]
    assert payload.observed_facts["ip_policy_range_classification"]["invalid_ranges"][0]["errors"] == ["invalid_start_address"]


# --- service evaluation -------------------------------------------------------


def test_unresolved_dependency_is_recorded_as_gap_and_action_without_ai_output():
    payload = evaluate_service_intent(service(), dependencies=[dependency()], ai_review_enabled=True)

    assert payload.target_type == "desired_service"
    assert payload.status == "partial"
    assert payload.observed_facts["ai_review"] == {"enabled": True, "executed": False}
    gap_codes = [gap["code"] for gap in payload.gap_summary["gaps"]]
    assert "unresolved_dependency" in gap_codes
    assert "service_observed_facts_unknown" in gap_codes
    assert payload.recommended_actions[0]["action"] == "resolve_service_dependency"
    assert payload.recommended_actions[0]["dependency"]["raw_ref"] == "component:default/database"


def test_service_with_provided_observed_facts_and_resolved_dependencies_is_satisfied():
    resolved = service(id="db-service", slug="database", name="database", display_name="Database", catalog_metadata_name="database")
    payload = evaluate_service_intent(
        service(),
        dependencies=[dependency(resolution_status="resolved", resolved_service_id=resolved.id)],
        resolved_services_by_id={resolved.id: resolved},
        observed_facts={"monitoring": {"status": "ok"}},
    )

    assert payload.status == "satisfied"
    assert payload.observed_facts["service_observation_status"] == "provided"
    assert payload.deterministic_summary["dependency_counts"]["resolved"] == 1
    assert payload.recommended_actions == []
