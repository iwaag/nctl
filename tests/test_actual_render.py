from __future__ import annotations

from nctl_core.actual_render import render_actual_data, render_actual_text
from nctl_core.output import Envelope
from nctl_core.sources.actual import (
    ActualCluster,
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


def _aghub_snapshot() -> ActualSnapshot:
    return ActualSnapshot(
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


def test_render_actual_text_shows_observer_cluster_and_guest_line():
    data = render_actual_data(_aghub_snapshot())
    envelope = Envelope.build("nctl.actual.v1", data)

    text = render_actual_text(envelope)

    assert "observer aghub-device-uuid" in text
    assert "cluster aghub-proxmox  complete  observed 2026-07-24T00:00:00+00:00" in text
    assert "lxc  vmid=108  agdnsmasq  running  node=aghub" in text
    assert "ok: True" in text


def test_render_actual_text_never_names_the_future_desired_cluster_slug():
    # plan.md Section 5.6: must not call the future actual Cluster "aghub-pve".
    data = render_actual_data(_aghub_snapshot())
    envelope = Envelope.build("nctl.actual.v1", data)

    assert "aghub-pve" not in render_actual_text(envelope)
    assert "aghub-pve" not in envelope.to_json()


def test_render_actual_data_handles_empty_snapshot():
    data = render_actual_data(ActualSnapshot())
    assert data.clusters == []
    assert data.read_errors == []
