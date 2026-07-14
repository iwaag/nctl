from __future__ import annotations

import httpx
import respx

from nctl_core.dnsmasq_query import DNSMASQ_QUERY, fetch_dnsmasq_inputs, latest_evaluations
from nctl_core.nautobot import NautobotClient

BASE_URL = "http://nautobot.test"


def test_latest_evaluations_keeps_highest_reviewed_at_per_target():
    rows = [
        {"target_id": "a", "reviewed_at": "2026-01-01T00:00:00+00:00", "created": "2026-01-01T00:00:00+00:00"},
        {"target_id": "a", "reviewed_at": "2026-02-01T00:00:00+00:00", "created": "2026-02-01T00:00:00+00:00"},
        {"target_id": "b", "reviewed_at": "2026-01-15T00:00:00+00:00", "created": "2026-01-15T00:00:00+00:00"},
    ]

    result = latest_evaluations(rows)

    assert result["a"]["reviewed_at"] == "2026-02-01T00:00:00+00:00"
    assert result["b"]["reviewed_at"] == "2026-01-15T00:00:00+00:00"


def test_latest_evaluations_breaks_reviewed_at_tie_with_created():
    rows = [
        {"target_id": "a", "reviewed_at": "2026-01-01T00:00:00+00:00", "created": "2026-01-01T00:00:00+00:00"},
        {"target_id": "a", "reviewed_at": "2026-01-01T00:00:00+00:00", "created": "2026-01-02T00:00:00+00:00"},
    ]

    result = latest_evaluations(rows)

    assert result["a"]["created"] == "2026-01-02T00:00:00+00:00"


def test_latest_evaluations_full_tie_keeps_first_occurrence():
    rows = [
        {"target_id": "a", "reviewed_at": "2026-01-01T00:00:00+00:00", "created": "2026-01-01T00:00:00+00:00", "marker": "first"},
        {"target_id": "a", "reviewed_at": "2026-01-01T00:00:00+00:00", "created": "2026-01-01T00:00:00+00:00", "marker": "second"},
    ]

    result = latest_evaluations(rows)

    assert result["a"]["marker"] == "first"


def test_latest_evaluations_handles_null_reviewed_at():
    rows = [
        {"target_id": "a", "reviewed_at": None, "created": "2026-01-01T00:00:00+00:00"},
        {"target_id": "a", "reviewed_at": "2026-01-02T00:00:00+00:00", "created": "2026-01-02T00:00:00+00:00"},
    ]

    result = latest_evaluations(rows)

    assert result["a"]["reviewed_at"] == "2026-01-02T00:00:00+00:00"


@respx.mock
def test_fetch_dnsmasq_inputs_lowercases_choice_fields_and_splits_evaluations():
    respx.post(f"{BASE_URL}/api/graphql/").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
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
                            "generate_dnsmasq": True,
                            "dnsmasq_record_type": "HOST_RECORD",
                            "desired_node": {"id": "node-1", "name": "Edge 1", "slug": "edge-1", "lifecycle": "ACTIVE"},
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
                    "endpoint_evaluations": [
                        {
                            "target_id": "endpoint-1",
                            "reviewed_at": "2026-06-01T00:00:00+00:00",
                            "created": "2026-06-01T00:00:00+00:00",
                            "observed_facts": {"dhcp_mac_candidates": []},
                            "deterministic_summary": {"dhcp_reservation_ready": True},
                            "actual_refs": [],
                        }
                    ],
                    "node_evaluations": [
                        {
                            "target_id": "node-1",
                            "reviewed_at": "2026-06-01T00:00:00+00:00",
                            "created": "2026-06-01T00:00:00+00:00",
                            "observed_facts": {},
                            "deterministic_summary": {},
                            "actual_refs": [{"object_type": "dcim.device", "id": "dev-1", "name": "edge-1.local"}],
                        }
                    ],
                }
            },
        )
    )
    client = NautobotClient(BASE_URL, "tok")

    result = fetch_dnsmasq_inputs(client)

    endpoint = result.endpoints[0]
    assert endpoint["endpoint_type"] == "primary"
    assert endpoint["ip_policy"] == "dhcp_reserved"
    assert endpoint["dnsmasq_record_type"] == "host_record"
    assert endpoint["desired_node"]["lifecycle"] == "active"
    assert endpoint["desired_node"]["slug"] == "edge-1"

    ip_range = result.ip_ranges[0]
    assert ip_range["range_policy"] == "dhcp_dynamic_pool"
    assert ip_range["lifecycle"] == "active"

    assert "endpoint-1" in result.endpoint_evaluations
    assert "node-1" in result.node_evaluations
    assert result.node_evaluations["node-1"]["actual_refs"][0]["object_type"] == "dcim.device"


def test_query_requests_target_type_filtered_evaluation_aliases():
    assert 'endpoint_evaluations: intent_evaluations(target_type: "desired_endpoint")' in DNSMASQ_QUERY
    assert 'node_evaluations: intent_evaluations(target_type: "desired_node")' in DNSMASQ_QUERY
