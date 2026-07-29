"""Focused P1 compute-realization matching and comparison cases (Tier B)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from nctl_core.compute.model import DesiredComputeInstance, DesiredComputePlatform
from nctl_core.drift.compute_evaluation import evaluate_compute
from nctl_core.drift.context import DriftContext
from nctl_core.sources.actual import ActualCluster, ActualSnapshot, ActualVirtualMachine, ActualVMInterface
from nctl_core.sources.desired import DesiredEndpoint, DesiredNode, DesiredSnapshot
from nctl_core.sources.snapshot import SourceSnapshot

NOW = "2026-07-28T12:00:00+00:00"


def _snapshot(*, cluster: dict | None = None, vm: dict | None = None, interfaces: list[dict] | None = None, link: bool = False) -> SourceSnapshot:
    node = DesiredNode(id="node", slug="guest", name="guest", lifecycle="active", node_type="device", realized_device_id="device")
    endpoint = DesiredEndpoint(id="endpoint", name="primary", endpoint_type="primary", node_id="node", node_slug="guest", ip_address="192.0.2.10/24", gateway_address="192.0.2.1", ip_policy="static", mdns_name="guest.local", mac_address="bc:24:11:23:dc:b7")
    platform = DesiredComputePlatform(id="platform", name="pve", slug="pve", provider_type="proxmox", lifecycle="active", control_node_id="node", config_schema_version="v1", config={"cluster_name": "cluster", "default_storage": "local-lvm", "default_bridge": "vmbr0"}, realized_cluster_id="cluster" if link else None)
    instance = DesiredComputeInstance(id="instance", desired_node_id="node", platform_id="platform", instance_kind="container", desired_power_state="running", vcpus=1, memory_mb=512, root_disk_gb=8, config_schema_version="v1", config={"vmid": 108, "template": "local:vztmpl/ubuntu.tar.zst", "storage": "local-lvm", "bridge": "vmbr0", "unprivileged": True}, realized_vm_id="vm" if link else None)
    cluster_data = {"id": "cluster", "name": "cluster", "proxmox": {"observer_device_id": "device", "observed_at": NOW, "observation_state": "complete", "observed_node_names": ["host"]}}
    cluster_data.update(cluster or {})
    vm_data = {"id": "vm", "name": "guest", "cluster_id": "cluster", "vcpus": 1, "memory": 512, "disk": 8, "proxmox": {"guest_type": "lxc", "vmid": 108, "node": "host", "status": "running", "lxc_rootfs": {"storage": "local-lvm", "size_gb": 8}}}
    vm_data.update(vm or {})
    return SourceSnapshot(desired=DesiredSnapshot(nodes=[node], endpoints=[endpoint], compute_platforms=[platform], compute_instances=[instance]), actual=ActualSnapshot(clusters=[ActualCluster.model_validate(cluster_data)], virtual_machines=[ActualVirtualMachine.model_validate(vm_data)], vm_interfaces=[ActualVMInterface.model_validate(value) for value in (interfaces or [{"id": "iface", "name": "net0", "virtual_machine_id": "vm", "mac_address": "BC:24:11:23:DC:B7", "proxmox": {"presence": "present", "bridge": "vmbr0"}}])]), fetched_at=datetime.now(timezone.utc))


def _codes(snapshot: SourceSnapshot) -> list[str]:
    return [record.code for record in evaluate_compute(snapshot, DriftContext(generated_at=NOW))]


def test_happy_vmid_match_is_unlinked_and_declared_only_fields_do_not_drift():
    codes = _codes(_snapshot())
    assert "compute_instance_not_linked" in codes
    assert "compute_realization_summary" in codes
    assert "compute_identity_conflict" not in codes
    assert "template" not in codes and "unprivileged" not in codes


@pytest.mark.parametrize(
    ("cluster", "expected"),
    [
        ({"id": "other"}, "compute_platform_missing"),
        ({"name": "other"}, "compute_identity_conflict"),
        ({"proxmox": {"observer_device_id": "device", "observed_at": "2026-07-20T00:00:00+00:00", "observation_state": "complete", "observed_node_names": ["host"]}}, "compute_platform_observation_stale"),
        ({"proxmox": {"observer_device_id": "device", "observed_at": NOW, "observation_state": "partial", "observed_node_names": ["host"]}}, "compute_platform_observation_stale"),
    ],
)
def test_platform_failures_are_reported(cluster, expected):
    assert expected in _codes(_snapshot(cluster=cluster, link=True))


@pytest.mark.parametrize(
    ("vm", "expected"),
    [
        ({"id": "other"}, "compute_realized_instance_missing"),
        ({"cluster_id": "other"}, "compute_identity_conflict"),
        ({"proxmox": {"guest_type": "qemu", "vmid": 108, "node": "host", "status": "running"}}, "compute_identity_conflict"),
        ({"proxmox": {"guest_type": "lxc", "vmid": 109, "node": "host", "status": "stopped", "lxc_rootfs": {"storage": "other", "size_gb": 7}}}, "compute_power_state_mismatch"),
    ],
)
def test_linked_guest_conflicts_and_comparisons_are_reported(vm, expected):
    assert expected in _codes(_snapshot(vm=vm, link=True))


def test_other_platform_guest_is_not_adopted():
    snapshot = _snapshot(vm={"cluster_id": "other"})
    assert "compute_instance_missing" in _codes(snapshot)


def test_multi_nic_suppresses_mac_and_bridge_comparison():
    interfaces = [
        {"id": "a", "name": "net0", "virtual_machine_id": "vm", "mac_address": "00:00:00:00:00:01", "proxmox": {"presence": "present", "bridge": "wrong"}},
        {"id": "b", "name": "net1", "virtual_machine_id": "vm", "mac_address": "00:00:00:00:00:02", "proxmox": {"presence": "present", "bridge": "wrong"}},
    ]
    codes = _codes(_snapshot(interfaces=interfaces))
    assert "compute_endpoint_mac_conflict" not in codes
    assert "compute_resource_mismatch" not in codes
