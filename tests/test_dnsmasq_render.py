import json

import httpx
import respx

from nctl_core.config import Config
from nctl_core.dnsmasq import dnsmasq_content_sha256
from nctl_core.dnsmasq_render import (
    build_dnsmasq_render,
    render_dnsmasq_conf_text,
    render_dnsmasq_summary_text,
)
from nctl_core.nautobot import NautobotConnectionError

BASE_URL = "http://nautobot.test"

EMPTY_DESIRED_RESPONSE = {
    "data": {
        "desired_nodes": [],
        "desired_endpoints": [],
        "desired_ip_ranges": [],
        "desired_node_operational_overrides": [],
        "desired_service_placements": [],
        "desired_services": [],
        "desired_dependencies": [],
    }
}

EMPTY_ACTUAL_RESPONSE = {
    "data": {
        "devices": [],
        "virtual_machines": [],
        "interfaces": [],
        "ip_addresses": [],
    }
}

ONE_ENDPOINT_DESIRED_RESPONSE = {
    "data": {
        "desired_nodes": [
            {
                "id": "node-1",
                "slug": "edge-1",
                "name": "Edge 1",
                "lifecycle": "ACTIVE",
                "node_type": "DEVICE",
                "role": None,
                "accepted_actual_types": ["DEVICE"],
                "expected_spec": {},
                "realized_device": None,
                "realized_vm": None,
            }
        ],
        "desired_endpoints": [
            {
                "id": "endpoint-1",
                "name": "primary",
                "endpoint_type": "PRIMARY",
                "ip_address": "192.0.2.10/32",
                "ip_policy": "STATIC",
                "dns_name": "edge-1.example.test",
                "mdns_name": "edge-1.local",
                "vpn_dns_name": None,
                "protocol": None,
                "port": None,
                "generate_dnsmasq": True,
                "dnsmasq_record_type": "HOST_RECORD",
                "realized_ip_address": None,
                "desired_node": {"id": "node-1", "slug": "edge-1"},
            }
        ],
        "desired_ip_ranges": [],
        "desired_node_operational_overrides": [],
        "desired_service_placements": [],
        "desired_services": [],
        "desired_dependencies": [],
    }
}


DESIRED_MAC_NO_ACTUAL_EVIDENCE_RESPONSE = {
    "data": {
        "desired_nodes": [
            {
                "id": "node-1",
                "slug": "edge-1",
                "name": "Edge 1",
                "lifecycle": "ACTIVE",
                "node_type": "DEVICE",
                "role": None,
                "accepted_actual_types": ["DEVICE"],
                "expected_spec": {},
                "realized_device": None,
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
                "mdns_name": None,
                "vpn_dns_name": None,
                "mac_address": "aa:bb:cc:dd:ee:ff",
                "protocol": None,
                "port": None,
                "generate_dnsmasq": True,
                "dnsmasq_record_type": "HOST_RECORD",
                "realized_ip_address": None,
                "desired_node": {"id": "node-1", "slug": "edge-1"},
            }
        ],
        "desired_ip_ranges": [],
        "desired_node_operational_overrides": [],
        "desired_service_placements": [],
        "desired_services": [],
        "desired_dependencies": [],
    }
}


def _desired_mac_mismatch_response(*, desired_mac: str, actual_mac: str) -> dict:
    return {
        "data": {
            "desired_nodes": [
                {
                    "id": "node-1",
                    "slug": "edge-1",
                    "name": "Edge 1",
                    "lifecycle": "ACTIVE",
                    "node_type": "DEVICE",
                    "role": None,
                    "accepted_actual_types": ["DEVICE"],
                    "expected_spec": {},
                    "realized_device": {"id": "device-1"},
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
                    "mdns_name": None,
                    "vpn_dns_name": None,
                    "mac_address": desired_mac,
                    "protocol": None,
                    "port": None,
                    "generate_dnsmasq": True,
                    "dnsmasq_record_type": "HOST_RECORD",
                    "realized_ip_address": None,
                    "desired_node": {"id": "node-1", "slug": "edge-1"},
                }
            ],
            "desired_ip_ranges": [],
            "desired_node_operational_overrides": [],
            "desired_service_placements": [],
            "desired_services": [],
            "desired_dependencies": [],
        }
    }


DEVICE_WITH_INTERFACE_ACTUAL_RESPONSE = {
    "data": {
        "devices": [
            {"id": "device-1", "name": "Edge 1", "serial": None, "platform": {"name": None}, "_custom_field_data": {}}
        ],
        "virtual_machines": [],
        "interfaces": [
            {"id": "iface-1", "name": "eth0", "mac_address": "11:22:33:44:55:66", "enabled": True, "device": {"id": "device-1"}}
        ],
        "ip_addresses": [],
    }
}


def make_config(tmp_path) -> Config:
    config_path = tmp_path / "nctl.toml"
    config_path.write_text(
        f"""
[nautobot]
url = "{BASE_URL}"

[inventory]
dumps_dir = "{tmp_path / 'dumps'}"

[ansible]
playbook_dir = "{tmp_path / 'ansible_agdev'}"
inventory = "inventories/generated/hosts_intent.yml"
"""
    )
    return Config.load(config_path)


def _mock_graphql(desired_response, actual_response=EMPTY_ACTUAL_RESPONSE):
    respx.post(f"{BASE_URL}/api/graphql/").mock(
        side_effect=[
            httpx.Response(200, json=desired_response),
            httpx.Response(200, json=actual_response),
        ]
    )


@respx.mock
def test_build_dnsmasq_render_ok_with_one_endpoint(tmp_path):
    _mock_graphql(ONE_ENDPOINT_DESIRED_RESPONSE)
    cfg = make_config(tmp_path)

    envelope = build_dnsmasq_render(cfg)

    assert envelope.ok is True
    assert envelope.schema_name == "nctl.render.dnsmasq.v3"
    assert envelope.data.schema_version == "5.0"
    assert envelope.data.summary["dns_records"] == 1
    assert "host-record=edge-1.example.test,192.0.2.10" in envelope.data.conf
    assert "# Generated by nctl" in envelope.data.conf
    assert "# operation_id" not in envelope.data.conf
    assert len(envelope.data.content_sha256) == 64


@respx.mock
def test_build_dnsmasq_render_empty_desired_state(tmp_path):
    _mock_graphql(EMPTY_DESIRED_RESPONSE)
    cfg = make_config(tmp_path)

    envelope = build_dnsmasq_render(cfg)

    assert envelope.ok is True
    assert envelope.data.summary["total_endpoints"] == 0
    assert envelope.data.dns_records == []


@respx.mock
def test_build_dnsmasq_render_never_embeds_operation_id_or_timestamp_in_conf(tmp_path):
    # fix_sshkey3 Step 3: generated_at/operation_id are deliberately kept out
    # of the deployed conf bytes -- they stay in the JSON envelope metadata
    # only, so equal desired state always renders byte-identical conf
    # regardless of when/which operation rendered it.
    _mock_graphql(EMPTY_DESIRED_RESPONSE)
    cfg = make_config(tmp_path)

    envelope = build_dnsmasq_render(cfg, operation_id="01JTESTOPERATION00000000000")

    assert "01JTESTOPERATION00000000000" not in envelope.data.conf
    assert "generated_at" not in envelope.data.conf
    assert envelope.data.conf == "# Generated by nctl\n# schema_version: 5.0\n"


def test_build_dnsmasq_render_degrades_on_nautobot_failure(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)

    class FailingClient:
        def __init__(self, *a, **kw):
            pass

        def graphql(self, *a, **kw):
            raise NautobotConnectionError("connection refused")

        def close(self):
            pass

    monkeypatch.setattr("nctl_core.dnsmasq_render.NautobotClient", FailingClient)

    envelope = build_dnsmasq_render(cfg)

    assert envelope.ok is False
    assert any(err.code == "nautobot_fetch_failed" for err in envelope.errors)


def test_render_dnsmasq_conf_text_returns_error_lines_when_not_ok(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)

    class FailingClient:
        def __init__(self, *a, **kw):
            pass

        def graphql(self, *a, **kw):
            raise NautobotConnectionError("connection refused")

        def close(self):
            pass

    monkeypatch.setattr("nctl_core.dnsmasq_render.NautobotClient", FailingClient)
    envelope = build_dnsmasq_render(cfg)

    text = render_dnsmasq_conf_text(envelope)

    assert "error [nautobot_fetch_failed]" in text


@respx.mock
def test_render_dnsmasq_summary_text_reports_counts(tmp_path):
    _mock_graphql(ONE_ENDPOINT_DESIRED_RESPONSE)
    cfg = make_config(tmp_path)
    envelope = build_dnsmasq_render(cfg)

    text = render_dnsmasq_summary_text(envelope)

    assert "dns_records: 1" in text


@respx.mock
def test_envelope_json_round_trips_expected_keys(tmp_path):
    _mock_graphql(ONE_ENDPOINT_DESIRED_RESPONSE)
    cfg = make_config(tmp_path)
    envelope = build_dnsmasq_render(cfg)

    parsed = json.loads(envelope.to_json())

    assert parsed["schema"] == "nctl.render.dnsmasq.v3"
    assert set(parsed["data"].keys()) == {
        "schema_version",
        "summary",
        "dns_records",
        "dhcp_reservations",
        "dhcp_ranges",
        "skipped",
        "conf",
        "content_sha256",
        "blocked",
        "blocking_findings",
        "partial_conf_preview",
    }


# --- VM p3 Step 6: desired MAC as a safe dnsmasq consumer, through the real
# SourceSnapshot -> compute_dnsmasq_render() path (fake GraphQL, not dict literals). ---


@respx.mock
def test_desired_mac_reservation_emitted_with_no_actual_evidence_at_all(tmp_path):
    """Rule 1: a complete desired endpoint with no endpoint evaluation/actual
    reference/Device/VM/interface at all still emits the reservation."""
    _mock_graphql(DESIRED_MAC_NO_ACTUAL_EVIDENCE_RESPONSE, EMPTY_ACTUAL_RESPONSE)
    cfg = make_config(tmp_path)

    envelope = build_dnsmasq_render(cfg)

    assert envelope.ok is True
    assert envelope.data.blocked is False
    reservation = envelope.data.dhcp_reservations[0]
    assert reservation["mac_address"] == "aa:bb:cc:dd:ee:ff"
    assert reservation["mac_source"] == "desired_endpoint"
    assert reservation["confidence"] == "deterministic_desired"
    assert reservation["actual_ref"] is None
    assert "dhcp-host=aa:bb:cc:dd:ee:ff,edge-1.example.test,192.0.2.10" in envelope.data.conf
    assert len(envelope.data.content_sha256) == 64


@respx.mock
def test_desired_mac_mismatch_blocks_the_whole_render(tmp_path):
    _mock_graphql(
        _desired_mac_mismatch_response(desired_mac="aa:bb:cc:dd:ee:ff", actual_mac="11:22:33:44:55:66"),
        DEVICE_WITH_INTERFACE_ACTUAL_RESPONSE,
    )
    cfg = make_config(tmp_path)

    envelope = build_dnsmasq_render(cfg)

    assert envelope.ok is False
    assert envelope.data.blocked is True
    assert envelope.data.conf == ""
    assert envelope.data.content_sha256 == ""
    assert any(err.code == "desired_mac_mismatch" for err in envelope.errors)
    finding = envelope.data.blocking_findings[0]
    assert finding["desired_mac"] == "aa:bb:cc:dd:ee:ff"
    assert finding["desired_node_slug"] == "edge-1"
    # The diagnostic preview is explicitly not the authoritative `conf` field.
    assert envelope.data.partial_conf_preview is not None
    assert envelope.data.partial_conf_preview != envelope.data.conf


@respx.mock
def test_desired_mac_mismatch_then_resolved_round_trip(tmp_path):
    """Item 8: a mismatch blocks the render; once desired/actual agree, the same
    snapshot shape renders a normal deployable reservation again."""
    cfg = make_config(tmp_path)

    _mock_graphql(
        _desired_mac_mismatch_response(desired_mac="aa:bb:cc:dd:ee:ff", actual_mac="11:22:33:44:55:66"),
        DEVICE_WITH_INTERFACE_ACTUAL_RESPONSE,
    )
    blocked_envelope = build_dnsmasq_render(cfg)
    assert blocked_envelope.ok is False
    assert blocked_envelope.data.blocked is True

    _mock_graphql(
        _desired_mac_mismatch_response(desired_mac="11:22:33:44:55:66", actual_mac="11:22:33:44:55:66"),
        DEVICE_WITH_INTERFACE_ACTUAL_RESPONSE,
    )
    resolved_envelope = build_dnsmasq_render(cfg)
    assert resolved_envelope.ok is True
    assert resolved_envelope.data.blocked is False
    assert resolved_envelope.data.content_sha256

    _mock_graphql(
        _desired_mac_mismatch_response(desired_mac="aa:bb:cc:dd:ee:ff", actual_mac="11:22:33:44:55:66"),
        DEVICE_WITH_INTERFACE_ACTUAL_RESPONSE,
    )
    blocked_again_envelope = build_dnsmasq_render(cfg)
    assert blocked_again_envelope.ok is False
    assert blocked_again_envelope.data.blocked is True


@respx.mock
def test_no_mac_fixture_render_is_byte_identical_regression(tmp_path):
    """Rule 4 hard requirement: an existing no-desired-MAC fixture's render
    output/digest is unaffected by this step."""
    _mock_graphql(ONE_ENDPOINT_DESIRED_RESPONSE)
    cfg = make_config(tmp_path)

    envelope = build_dnsmasq_render(cfg)

    assert envelope.ok is True
    assert envelope.data.blocked is False
    assert envelope.data.conf == "# Generated by nctl\n# schema_version: 5.0\nhost-record=edge-1.example.test,192.0.2.10\n"
    assert envelope.data.content_sha256 == dnsmasq_content_sha256(envelope.data.conf)
