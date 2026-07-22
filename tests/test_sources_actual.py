from __future__ import annotations

import httpx
import respx

from nctl_core.nautobot import NautobotClient
from nctl_core.sources.actual import (
    ACTUAL_QUERY,
    ActualFacts,
    actual_type_problem,
    fetch_actual_snapshot,
    missing_required_facts,
    read_actual_facts,
)

BASE_URL = "http://nautobot.test"


def test_read_actual_facts_reads_only_the_allowlist():
    facts = read_actual_facts(
        {
            "host_system": "linux",
            "primary_ip_address": "192.168.0.10",
            "primary_mac_address": "aa:bb:cc:dd:ee:ff",
            "network_interface": "eth0",
            "last_seen": "2026-07-14T00:00:00+00:00",
            "inventory_source": "nodeutils",
            "observed_services": {"nomad": {"state": "running", "source": "systemd"}},
            "service_inventory_updated_at": "2026-07-14T00:01:00+00:00",
            "inventory_raw_json": {"anything": "ignored"},
            "cpu_model": "ignored too",
        }
    )
    assert facts == ActualFacts(
        observed_system="linux",
        local_ip="192.168.0.10",
        mac_address="aa:bb:cc:dd:ee:ff",
        network_interface="eth0",
        collected_at="2026-07-14T00:00:00+00:00",
        inventory_source="nodeutils",
        observed_services={"nomad": {"state": "running", "source": "systemd"}},
        service_inventory_updated_at="2026-07-14T00:01:00+00:00",
    )


def test_read_actual_facts_preserves_nested_managed_file_metadata_unchanged():
    # fix_sshkey3 Step 4: observed_services[*].managed_files must survive
    # read_actual_facts() (and therefore GraphQL parsing) structurally
    # unchanged -- no field renaming, flattening, or content extraction.
    managed_files = {
        "records": {
            "path": "/etc/dnsmasq.d/nintent-records.conf",
            "status": "present",
            "sha256": "a" * 64,
            "size": 1234,
            "checked_at": "2026-07-22T00:00:00+00:00",
        }
    }
    facts = read_actual_facts(
        {
            "observed_services": {
                "dnsmasq": {"state": "active", "source": "systemd", "managed_files": managed_files},
            },
        }
    )
    assert facts.observed_services["dnsmasq"]["managed_files"] == managed_files


def test_read_actual_facts_handles_missing_and_blank_values():
    facts = read_actual_facts({"host_system": "  "})
    assert facts.observed_system is None
    assert facts.local_ip is None


def test_actual_type_problem():
    assert actual_type_problem(None) == "no_realized_device"
    assert actual_type_problem("device") is None
    assert actual_type_problem("virtual_machine") == "unsupported_actual_type"


def test_missing_required_facts_only_checks_requested_consumers():
    facts = ActualFacts(
        observed_system=None,
        local_ip=None,
        mac_address=None,
        network_interface="eth0",
        collected_at=None,
        inventory_source=None,
    )
    assert missing_required_facts(facts, {"host_os"}) == ["missing_observed_system"]
    assert missing_required_facts(facts, {"network_interface"}) == []
    assert missing_required_facts(facts, {"host_os", "wol"}) == [
        "missing_mac_address",
        "missing_observed_system",
    ]


@respx.mock
def test_fetch_actual_snapshot_reads_custom_field_data_and_relations():
    respx.post(f"{BASE_URL}/api/graphql/").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "devices": [
                        {
                            "id": "dev-1",
                            "name": "agpc",
                            "serial": "SER123",
                            "platform": {"name": "ubuntu"},
                            "_custom_field_data": {
                                "host_system": "linux",
                                "primary_mac_address": "aa:bb:cc:dd:ee:ff",
                                "primary_ip_address": "192.168.0.110",
                                "network_interface": "eth0",
                                "last_seen": "2026-07-14T00:00:00+00:00",
                                "inventory_source": "nodeutils",
                                "observed_services": {"nomad": {"state": "running"}},
                                "service_inventory_updated_at": "2026-07-14T00:01:00+00:00",
                            },
                        }
                    ],
                    "virtual_machines": [{"id": "vm-1", "name": "svc-1"}],
                    "interfaces": [
                        {
                            "id": "iface-1",
                            "name": "eth0",
                            "mac_address": "aa:bb:cc:dd:ee:ff",
                            "enabled": True,
                            "device": {"id": "dev-1"},
                        }
                    ],
                    "ip_addresses": [
                        {
                            "id": "ip-1",
                            "host": "192.168.0.110",
                            "mask_length": 24,
                            "dns_name": "agpc.example.test",
                            "interfaces": [{"id": "iface-1"}],
                        }
                    ],
                }
            },
        )
    )
    client = NautobotClient(BASE_URL, "tok")

    snapshot = fetch_actual_snapshot(client)

    device = snapshot.devices[0]
    assert device.name == "agpc"
    assert device.serial == "SER123"
    assert device.platform == "ubuntu"
    assert device.actual_facts().observed_system == "linux"
    assert device.actual_facts().observed_services["nomad"]["state"] == "running"
    assert device.actual_facts().service_inventory_updated_at == "2026-07-14T00:01:00+00:00"

    assert snapshot.virtual_machines[0].name == "svc-1"

    interface = snapshot.interfaces[0]
    assert interface.mac_address == "aa:bb:cc:dd:ee:ff"
    assert interface.enabled is True
    assert interface.device_id == "dev-1"

    ip_address = snapshot.ip_addresses[0]
    assert ip_address.host == "192.168.0.110"
    assert ip_address.dns_name == "agpc.example.test"
    assert ip_address.interface_ids == ["iface-1"]


def test_query_requests_custom_field_data_not_shortcut_fields():
    # host_system/network_interface have no registered CustomField definition
    # on the live schema, so cf_* shortcuts don't exist for them; the raw JSON
    # blob is the only way to read all eight allowlisted fields in one place.
    assert "_custom_field_data" in ACTUAL_QUERY
    assert "cf_host_system" not in ACTUAL_QUERY
