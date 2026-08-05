from __future__ import annotations

from nctl_core.actual_render import ACTUAL_SCHEMA, render_actual_data, render_actual_text
from nctl_core.output import Envelope
from nctl_core.sources.actual import (
    ActualCluster,
    ActualDevice,
    ActualIPAddress,
    ActualSnapshot,
    ActualVirtualMachine,
    ActualVMInterface,
    ProxmoxClusterFacts,
    ProxmoxLxcRootfs,
    ProxmoxManagedIpEntry,
    ProxmoxManagedIpEvidence,
    ProxmoxVirtualMachineFacts,
    ProxmoxVMInterfaceFacts,
)


# The raw nodeutils facts dict exactly as nauto's ingest stores it under the
# `inventory_raw_json` Device custom field: {"facts": <nodeutils facts>, ...}.
_RAW_NODEUTILS_FACTS = {
    "gpu": {"model": "NVIDIA RTX A2000", "vram_mb": 6144},
    "memory": {"total_mb": 65536},
    "containers": [{"name": "nomad-client", "state": "running"}],
    "cpu_model": "AMD Ryzen 9",
}


def _agpc_device(raw: bool = True) -> ActualDevice:
    facts: dict = {
        "host_system": "linux",
        "primary_ip_address": "192.168.0.110",
        "primary_mac_address": "aa:bb:cc:dd:ee:ff",
        "last_seen": "2026-07-24T00:00:00+00:00",
        "inventory_source": "nodeutils",
    }
    if raw:
        facts["inventory_raw_json"] = {"facts": _RAW_NODEUTILS_FACTS, "collected_at": "2026-07-24T00:00:00+00:00"}
    return ActualDevice(id="dev-agpc", name="agpc", serial="SER110", platform="ubuntu", facts=facts)


def _aghub_snapshot() -> ActualSnapshot:
    return ActualSnapshot(
        devices=[_agpc_device()],
        clusters=[
            ActualCluster(
                id="cluster-1",
                name="aghub-proxmox",
                cluster_type="Proxmox VE",
                proxmox=ProxmoxClusterFacts(
                    scope_key="standalone-device:aghub-device-uuid",
                    identity_source="standalone_node_fallback",
                    observer_device_id="aghub-device-uuid",
                    observed_node_names=["aghub"],
                    node_count=1,
                    observed_at="2026-07-24T00:00:00+00:00",
                    observation_state="complete",
                ),
            )
        ],
        virtual_machines=[
            ActualVirtualMachine(
                id="vm-108",
                name="agdnsmasq",
                cluster_id="cluster-1",
                status="Active",
                role="lxc-container",
                vcpus=1,
                memory=512,
                disk=8,
                proxmox=ProxmoxVirtualMachineFacts(
                    guest_type="lxc",
                    vmid=108,
                    node="aghub",
                    status="running",
                    observed_at="2026-07-24T00:00:00+00:00",
                    observation_state="complete",
                    lxc_rootfs=ProxmoxLxcRootfs(storage="local-lvm", volume="vm-108-disk-0", size_gb=8),
                ),
            )
        ],
        vm_interfaces=[
            ActualVMInterface(
                id="iface-108-net0",
                name="net0",
                mac_address="aa:bb:cc:dd:ee:01",
                virtual_machine_id="vm-108",
                proxmox=ProxmoxVMInterfaceFacts(
                    config_slot="net0",
                    bridge="vmbr0",
                    interface_source="lxc_config",
                    observed_at="2026-07-24T00:00:00+00:00",
                    presence="present",
                    managed_ip_evidence=ProxmoxManagedIpEvidence(
                        managed={
                            "192.168.0.108/24": ProxmoxManagedIpEntry(
                                ip_id="ip-108", evidence_observed_at="2026-07-24T00:00:00+00:00"
                            )
                        },
                        evidence_observed_at="2026-07-24T00:00:00+00:00",
                    ),
                ),
            )
        ],
        ip_addresses=[
            ActualIPAddress(id="ip-108", host="192.168.0.108", mask_length=24, vm_interface_ids=["iface-108-net0"]),
            # Unrelated ledger IP relation on the same VMInterface, not in the managed set --
            # must be visible but never counted as fresh Proxmox-observed evidence.
            ActualIPAddress(id="ip-foreign", host="10.0.0.9", mask_length=32, vm_interface_ids=["iface-108-net0"]),
        ],
    )


def test_render_actual_data_builds_observer_cluster_guest_graph():
    data = render_actual_data(_aghub_snapshot())

    assert len(data.clusters) == 1
    cluster = data.clusters[0]
    assert cluster.name == "aghub-proxmox"
    assert cluster.identity_source == "standalone_node_fallback"
    assert cluster.observer_device_id == "aghub-device-uuid"
    assert cluster.observation_state == "complete"

    assert len(cluster.guests) == 1
    guest = cluster.guests[0]
    assert guest.guest_type == "lxc"
    assert guest.vmid == 108
    assert guest.name == "agdnsmasq"
    assert guest.node == "aghub"
    assert guest.proxmox_status == "running"
    assert guest.rootfs == {"storage": "local-lvm", "volume": "vm-108-disk-0", "size_gb": 8}


def test_render_actual_data_marks_managed_vs_unrelated_ip_relations():
    data = render_actual_data(_aghub_snapshot())
    iface = data.clusters[0].guests[0].interfaces[0]

    assert iface.config_slot == "net0"
    assert iface.managed_ip_count == 1
    assert iface.unrelated_ip_ids == ["ip-foreign"]


def test_render_actual_data_classifies_native_mask_mismatch_as_managed_by_id():
    # sidefix2/plan.md Section 4.4: a shared IPAddress's native mask_length (here /32, e.g. an
    # unrelated pre-existing DNS-facing row) is not the same thing as the exact observed-prefix
    # evidence key (here /24) nauto records after adopting it. Classification must stay ID-based
    # and must never read this mask mismatch as "unrelated".
    snapshot = _aghub_snapshot()
    snapshot.ip_addresses[0] = ActualIPAddress(
        id="ip-108", host="192.168.0.108", mask_length=32, vm_interface_ids=["iface-108-net0"]
    )

    data = render_actual_data(snapshot)
    iface = data.clusters[0].guests[0].interfaces[0]

    assert iface.managed_ip_count == 1
    assert iface.unrelated_ip_ids == ["ip-foreign"]


def test_render_actual_text_shows_observer_cluster_and_guest_line():
    data = render_actual_data(_aghub_snapshot())
    envelope = Envelope.build(ACTUAL_SCHEMA, data)

    text = render_actual_text(envelope)

    assert "device agpc  system=linux  ip=192.168.0.110  collected 2026-07-24T00:00:00+00:00" in text
    assert "observer aghub-device-uuid" in text
    assert "cluster aghub-proxmox  complete  observed 2026-07-24T00:00:00+00:00" in text
    assert "lxc  vmid=108  agdnsmasq  running  node=aghub" in text
    assert "ok: True" in text


def test_render_actual_text_never_names_the_future_desired_cluster_slug():
    # plan.md Section 5.6: must not call the future actual Cluster "aghub-pve".
    data = render_actual_data(_aghub_snapshot())
    envelope = Envelope.build(ACTUAL_SCHEMA, data)

    assert "aghub-pve" not in render_actual_text(envelope)
    assert "aghub-pve" not in envelope.to_json()


def test_render_actual_data_handles_empty_snapshot():
    data = render_actual_data(ActualSnapshot())
    assert data.devices == []
    assert data.clusters == []
    assert data.read_errors == []


def test_render_actual_data_detail_passes_raw_nodeutils_facts_through_unmodified():
    data = render_actual_data(_aghub_snapshot(), detail=True)

    assert data.detail_level == "raw"
    device = data.devices[0]
    assert device.name == "agpc"
    assert device.serial == "SER110"
    assert device.facts.observed_system == "linux"
    # GPU/memory/container keys arrive exactly as nauto stored them, nothing cherry-picked.
    assert device.facts_raw == _RAW_NODEUTILS_FACTS


def test_render_actual_data_without_detail_has_devices_but_no_raw_facts():
    data = render_actual_data(_aghub_snapshot())

    assert data.detail_level == "basic"
    device = data.devices[0]
    assert device.facts.local_ip == "192.168.0.110"
    assert device.facts_raw is None
    dump = data.model_dump_json()
    assert "NVIDIA" not in dump
    assert "cpu_model" not in dump


def test_render_actual_data_detail_yields_null_raw_facts_when_never_ingested():
    snapshot = _aghub_snapshot()
    snapshot.devices = [_agpc_device(raw=False)]

    data = render_actual_data(snapshot, detail=True)

    assert data.devices[0].facts_raw is None


def test_render_actual_data_host_scopes_devices_section_only():
    snapshot = _aghub_snapshot()
    other = _agpc_device(raw=False)
    snapshot.devices = [snapshot.devices[0], other.model_copy(update={"id": "dev-agstudio", "name": "agstudio"})]

    data = render_actual_data(snapshot, detail=True, host="agpc")

    assert [d.name for d in data.devices] == ["agpc"]
    # The cluster graph is deliberately not filtered by HOST.
    assert len(data.clusters) == 1


def test_render_actual_data_unknown_host_yields_empty_devices():
    data = render_actual_data(_aghub_snapshot(), host="no-such-host")
    assert data.devices == []


def test_build_actual_unknown_host_reports_named_error(monkeypatch):
    from types import SimpleNamespace

    import nctl_core.actual_render as actual_render

    monkeypatch.setattr(
        actual_render, "NautobotClient", lambda url, token: SimpleNamespace(close=lambda: None)
    )
    monkeypatch.setattr(actual_render, "fetch_actual_snapshot", lambda client: _aghub_snapshot())
    cfg = SimpleNamespace(nautobot=SimpleNamespace(url="http://nautobot.test", resolve_token=lambda: "tok"))

    envelope = actual_render.build_actual(cfg, host="no-such-host")

    assert envelope.ok is False
    assert any(error.code == "unknown_host" for error in envelope.errors)
