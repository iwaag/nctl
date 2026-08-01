from __future__ import annotations

import httpx
import respx

from nctl_core.nautobot import NautobotClient
from nctl_core.sources.actual import (
    ACTUAL_QUERY,
    ActualFacts,
    fetch_actual_snapshot,
    read_actual_facts,
)

BASE_URL = "http://nautobot.test"

# Phase 2 Step 6 gate fixture (plan.md Section 7 Step 6 gate / Section 8.2 "nctl" row):
# a synthetic LXC guest, VMID 101, under an example Proxmox cluster, exactly as
# nauto/jobs/proxmox_upsert.py + proxmox_interfaces.py would have written it.
_EXAMPLE_CLUSTER_ROW = {
    "id": "cluster-1",
    "name": "example-proxmox",
    "cluster_type": {"name": "Proxmox VE"},
    "_custom_field_data": {
        "proxmox_scope_key": "standalone-device:example-host-uuid",
        "proxmox_identity_source": "standalone_node_fallback",
        "proxmox_observer_device_id": "example-host-uuid",
        "proxmox_observed_node_names": ["example-host"],
        "proxmox_node_count": 1,
        "proxmox_observed_at": "2026-07-24T00:00:00+00:00",
        "proxmox_observation_state": "complete",
        "proxmox_observation_detail": {"state": "complete", "omitted_error_count": 0, "errors": []},
        "proxmox_storage_content": {
            "example-host:local:vztmpl": {
                "node": "example-host",
                "storage": "local",
                "content_type": "vztmpl",
                "state": "complete",
                "last_attempted_at": "2026-07-24T00:00:00+00:00",
                "evidence_observed_at": "2026-07-24T00:00:00+00:00",
                "omitted_error_count": 0,
                "errors": [],
                "items": [{"volid": "local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst", "content": "vztmpl", "format": "tzst", "size_bytes": 123}],
            }
        },
        # Unrelated custom-field key present on the same object -- must never enter the
        # typed output (Section 5.6: "Unrelated custom fields ... are ignored").
        "inventory_raw_json": {"anything": "must not leak"},
        "some_other_unrelated_field": "ignored",
    },
}

_EXAMPLE_VM_ROW = {
    "id": "vm-101",
    "name": "example-dns",
    "cluster": {"id": "cluster-1"},
    "status": {"name": "Active"},
    "role": {"name": "lxc-container"},
    "vcpus": 1,
    "memory": 512,
    "disk": 8,
    "_custom_field_data": {
        "proxmox_guest_type": "lxc",
        "proxmox_vmid": 101,
        "proxmox_node": "example-host",
        "proxmox_status": "running",
        "proxmox_observed_at": "2026-07-24T00:00:00+00:00",
        "proxmox_observation_state": "complete",
        "proxmox_observation_detail": {"state": "complete", "omitted_error_count": 0, "errors": []},
        "proxmox_lxc_rootfs": {"storage": "local-lvm", "volume": "vm-101-disk-0", "size_gb": 8},
        "proxmox_presence": "present",
        "proxmox_interface_evidence": {},
        "inventory_raw_json": {"anything": "must not leak"},
    },
}

_EXAMPLE_VMINTERFACE_ROW = {
    "id": "iface-101-net0",
    "name": "net0",
    "mac_address": "aa:bb:cc:dd:ee:01",
    "virtual_machine": {"id": "vm-101"},
    "_custom_field_data": {
        "proxmox_config_slot": "net0",
        "proxmox_guest_interface_name": None,
        "proxmox_bridge": "vmbr0",
        "proxmox_interface_source": "lxc_config",
        "proxmox_observed_at": "2026-07-24T00:00:00+00:00",
        "proxmox_presence": "present",
        "proxmox_managed_ip_evidence": {
            "managed": {"192.0.2.101/24": {"ip_id": "ip-101", "evidence_observed_at": "2026-07-24T00:00:00+00:00"}},
            "evidence_observed_at": "2026-07-24T00:00:00+00:00",
        },
    },
}


def _graphql_payload(**overrides):
    base = {
        "devices": [],
        "clusters": [_EXAMPLE_CLUSTER_ROW],
        "virtual_machines": [_EXAMPLE_VM_ROW],
        "vm_interfaces": [_EXAMPLE_VMINTERFACE_ROW],
        "interfaces": [],
        "ip_addresses": [],
    }
    base.update(overrides)
    return base


def test_read_actual_facts_reads_only_the_allowlist():
    facts = read_actual_facts(
        {
            "host_system": "linux",
            "primary_ip_address": "192.0.2.10",
            "primary_mac_address": "aa:bb:cc:dd:ee:ff",
            "network_interface": "eth0",
            "last_seen": "2026-07-14T00:00:00+00:00",
            "inventory_source": "nodeutils",
            "observed_services": {"nomad": {"state": "running", "source": "systemd"}},
            "service_inventory_updated_at": "2026-07-14T00:01:00+00:00",
            "observed_workspaces": {"pj-voxel3dprint": {"present": True, "dirty": False}},
            "inventory_raw_json": {"anything": "ignored"},
            "cpu_model": "ignored too",
        }
    )
    assert facts == ActualFacts(
        observed_system="linux",
        local_ip="192.0.2.10",
        mac_address="aa:bb:cc:dd:ee:ff",
        network_interface="eth0",
        collected_at="2026-07-14T00:00:00+00:00",
        inventory_source="nodeutils",
        observed_services={"nomad": {"state": "running", "source": "systemd"}},
        service_inventory_updated_at="2026-07-14T00:01:00+00:00",
        observed_workspaces={"pj-voxel3dprint": {"present": True, "dirty": False}},
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
    assert facts.observed_workspaces is None


def test_read_actual_facts_drops_malformed_workspace_entries():
    facts = read_actual_facts(
        {
            "observed_workspaces": {
                "pj-voxel3dprint": {"present": True, "path": "/home/eiji/projects/pj-voxel3dprint"},
                "": {"present": True},
                "bogus": "not-a-dict",
                None: {"present": True},
            }
        }
    )
    assert facts.observed_workspaces == {
        "pj-voxel3dprint": {"present": True, "path": "/home/eiji/projects/pj-voxel3dprint"}
    }


def test_read_actual_facts_observed_workspaces_not_a_mapping():
    facts = read_actual_facts({"observed_workspaces": ["not", "a", "mapping"]})
    assert facts.observed_workspaces is None


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
                            "name": "example-pc",
                            "serial": "SER123",
                            "platform": {"name": "ubuntu"},
                            "_custom_field_data": {
                                "host_system": "linux",
                                "primary_mac_address": "aa:bb:cc:dd:ee:ff",
                                "primary_ip_address": "192.0.2.110",
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
                            "host": "192.0.2.110",
                            "mask_length": 24,
                            "dns_name": "example-pc.example.test",
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
    assert device.name == "example-pc"
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
    assert ip_address.host == "192.0.2.110"
    assert ip_address.dns_name == "example-pc.example.test"
    assert ip_address.interface_ids == ["iface-1"]


def test_query_requests_custom_field_data_not_shortcut_fields():
    # host_system/network_interface have no registered CustomField definition
    # on the live schema, so cf_* shortcuts don't exist for them; the raw JSON
    # blob is the only way to read all eight allowlisted fields in one place.
    assert "_custom_field_data" in ACTUAL_QUERY
    assert "cf_host_system" not in ACTUAL_QUERY


def test_query_pins_dedicated_vm_interfaces_root_not_dcim_interfaces():
    # Step 0 (report2.0.md) found a separate VMInterface REST endpoint
    # (`virtualization/interfaces/`) from the DCIM Device interface endpoint; Section 5.6
    # requires the query not assume the `interfaces` root also contains VMInterfaces.
    assert "vm_interfaces" in ACTUAL_QUERY
    assert "clusters" in ACTUAL_QUERY


@respx.mock
def test_fetch_actual_snapshot_reads_example_proxmox_cluster_and_guest_vmid_101():
    respx.post(f"{BASE_URL}/api/graphql/").mock(return_value=httpx.Response(200, json={"data": _graphql_payload()}))
    client = NautobotClient(BASE_URL, "tok")

    snapshot = fetch_actual_snapshot(client)

    assert snapshot.proxmox_read_errors == []
    cluster = snapshot.clusters[0]
    assert cluster.name == "example-proxmox"
    assert cluster.cluster_type == "Proxmox VE"
    assert cluster.proxmox.identity_source == "standalone_node_fallback"
    assert cluster.proxmox.scope_key == "standalone-device:example-host-uuid"
    assert cluster.proxmox.observed_node_names == ["example-host"]
    assert cluster.proxmox.observation_state == "complete"
    scope = cluster.proxmox.storage_content["example-host:local:vztmpl"]
    assert scope.state == "complete"
    assert scope.items[0].volid == "local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst"

    vm = snapshot.virtual_machines[0]
    assert vm.name == "example-dns"
    assert vm.cluster_id == "cluster-1"
    assert vm.proxmox.guest_type == "lxc"
    assert vm.proxmox.vmid == 101
    assert vm.proxmox.node == "example-host"
    assert vm.proxmox.presence == "present"
    assert vm.proxmox.lxc_rootfs.volume == "vm-101-disk-0"

    iface = snapshot.vm_interfaces[0]
    assert iface.virtual_machine_id == "vm-101"
    assert iface.proxmox.config_slot == "net0"
    assert iface.proxmox.interface_source == "lxc_config"
    assert iface.proxmox.managed_ip_evidence.managed["192.0.2.101/24"].ip_id == "ip-101"


@respx.mock
def test_fetch_actual_snapshot_never_leaks_unrelated_or_raw_custom_data():
    respx.post(f"{BASE_URL}/api/graphql/").mock(return_value=httpx.Response(200, json={"data": _graphql_payload()}))
    client = NautobotClient(BASE_URL, "tok")

    snapshot = fetch_actual_snapshot(client)

    cluster_dump = snapshot.clusters[0].model_dump_json()
    vm_dump = snapshot.virtual_machines[0].model_dump_json()
    assert "inventory_raw_json" not in cluster_dump
    assert "must not leak" not in cluster_dump
    assert "inventory_raw_json" not in vm_dump
    assert "must not leak" not in vm_dump
    assert "some_other_unrelated_field" not in cluster_dump


@respx.mock
def test_fetch_actual_snapshot_reports_malformed_proxmox_json_as_structured_error():
    payload = _graphql_payload(
        clusters=[
            {
                **_EXAMPLE_CLUSTER_ROW,
                "id": "cluster-bad",
                "_custom_field_data": {
                    **_EXAMPLE_CLUSTER_ROW["_custom_field_data"],
                    # Unknown nested key inside the dedicated proxmox_observation_detail
                    # JSON must fail the strict extra="forbid" model, not pass silently.
                    "proxmox_observation_detail": {"state": "complete", "unexpected_key": "boom"},
                },
            }
        ],
        virtual_machines=[],
        vm_interfaces=[],
    )
    respx.post(f"{BASE_URL}/api/graphql/").mock(return_value=httpx.Response(200, json={"data": payload}))
    client = NautobotClient(BASE_URL, "tok")

    snapshot = fetch_actual_snapshot(client)

    assert snapshot.clusters[0].proxmox is None
    assert len(snapshot.proxmox_read_errors) == 1
    error = snapshot.proxmox_read_errors[0]
    assert error.object_type == "cluster"
    assert error.object_id == "cluster-bad"


@respx.mock
def test_fetch_actual_snapshot_drops_only_malformed_storage_scope():
    payload = _graphql_payload(
        clusters=[
            {
                **_EXAMPLE_CLUSTER_ROW,
                "_custom_field_data": {
                    **_EXAMPLE_CLUSTER_ROW["_custom_field_data"],
                    "proxmox_storage_content": {
                        **_EXAMPLE_CLUSTER_ROW["_custom_field_data"]["proxmox_storage_content"],
                        "bad": {"node": "example-host", "unexpected": True},
                    },
                },
            }
        ]
    )
    respx.post(f"{BASE_URL}/api/graphql/").mock(return_value=httpx.Response(200, json={"data": payload}))

    snapshot = fetch_actual_snapshot(NautobotClient(BASE_URL, "tok"))

    facts = snapshot.clusters[0].proxmox
    assert facts is not None
    assert facts.observation_state == "complete"
    assert set(facts.storage_content) == {"example-host:local:vztmpl"}
    assert facts.storage_content_invalid_scope_count == 1
    assert snapshot.proxmox_read_errors == []


@respx.mock
def test_fetch_actual_snapshot_leaves_proxmox_none_when_no_dedicated_fields_present():
    payload = _graphql_payload(
        clusters=[],
        virtual_machines=[{"id": "vm-plain", "name": "plain", "cluster": None, "status": None, "role": None,
                            "vcpus": None, "memory": None, "disk": None, "_custom_field_data": {}}],
        vm_interfaces=[],
    )
    respx.post(f"{BASE_URL}/api/graphql/").mock(return_value=httpx.Response(200, json={"data": payload}))
    client = NautobotClient(BASE_URL, "tok")

    snapshot = fetch_actual_snapshot(client)

    assert snapshot.virtual_machines[0].proxmox is None
    assert snapshot.proxmox_read_errors == []


def test_existing_device_and_interface_consumers_are_unaffected_by_step6_additions():
    # Section 2 exit criteria / Step 6 sub-item 4: Step 6 is additive only.
    from nctl_core.sources.actual import ActualDevice, ActualInterface, ActualSnapshot

    snapshot = ActualSnapshot(
        devices=[ActualDevice(id="d1", name="example-pc")],
        interfaces=[ActualInterface(id="i1", name="eth0")],
    )
    assert snapshot.devices[0].name == "example-pc"
    assert snapshot.interfaces[0].name == "eth0"
    assert snapshot.clusters == []
    assert snapshot.vm_interfaces == []
