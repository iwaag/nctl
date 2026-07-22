from __future__ import annotations

from nctl_core.dumps import NodeDump
from nctl_core.sources.observed import read_observed_facts


def _dump(facts: dict, serial_number: str | None = None) -> NodeDump:
    identity: dict = {"hostname": "agpc"}
    if serial_number is not None:
        identity["serial_number"] = serial_number
    return NodeDump.model_validate(
        {
            "schema_version": "nodeutils.inventory.v2",
            "identity": identity,
            "collected_at": "2026-07-14T12:00:00+00:00",
            "facts": facts,
            "self_reported": {},
        }
    )


def test_read_observed_facts_reads_system_and_primary_network():
    dump = _dump(
        {
            "system": "linux",
            "network": {
                "primary_mac_address": "aa:bb:cc:dd:ee:ff",
                "primary_ip_address": "192.168.0.110",
                "primary_interface": {"name": "eth0"},
            },
        },
        serial_number="SN123",
    )

    observed = read_observed_facts(dump)

    assert observed.hostname == "agpc"
    assert observed.serial_number == "SN123"
    assert observed.system == "linux"
    assert observed.primary_mac_address == "aa:bb:cc:dd:ee:ff"
    assert observed.primary_ip_address == "192.168.0.110"
    assert observed.primary_interface == "eth0"


def test_read_observed_facts_tolerates_missing_network_section():
    dump = _dump({"system": "macos"})

    observed = read_observed_facts(dump)

    assert observed.system == "macos"
    assert observed.primary_mac_address is None
    assert observed.primary_interface is None
    assert observed.serial_number is None
