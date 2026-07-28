"""Compute realization is visible in drift but remains non-actuating in P1."""

from __future__ import annotations

from datetime import datetime, timezone

from nctl_core.drift.context import DriftContext
from nctl_core.drift.engine import compute_drift
from nctl_core.reconcile.model import PlanScope
from nctl_core.reconcile.planner import build_plan
from nctl_core.sources.actual import ActualCluster, ActualDevice, ActualSnapshot, ActualVirtualMachine
from nctl_core.compute.model import DesiredComputeInstance, DesiredComputePlatform
from nctl_core.sources.desired import DesiredNode, DesiredSnapshot
from nctl_core.sources.snapshot import SourceSnapshot

GENERATED_AT = "2026-07-25T12:00:00+00:00"


def _snapshot_with_valid_compute() -> SourceSnapshot:
    node = DesiredNode(
        id="n1", slug="aghealthy", name="aghealthy", lifecycle="active", node_type="device",
        realized_device_id="dev-1",
    )
    device = ActualDevice(id="dev-1", name="aghealthy.local")
    platform = DesiredComputePlatform(
        id="platform-1", name="aghub-pve", slug="aghub-pve", provider_type="proxmox", lifecycle="planned",
        control_node_id="n1", config_schema_version="v1",
        config={"cluster_name": "aghub", "default_storage": "local-lvm", "default_bridge": "vmbr0"},
    )
    instance = DesiredComputeInstance(
        id="instance-1", desired_node_id="n1", platform_id="platform-1", instance_kind="container",
        vcpus=2, memory_mb=2048, root_disk_gb=20, config_schema_version="v1",
        config={"template": "local:vztmpl/debian-12.tar.zst", "unprivileged": True},
    )
    return SourceSnapshot(
        desired=DesiredSnapshot(nodes=[node], compute_platforms=[platform], compute_instances=[instance]),
        actual=ActualSnapshot(
            devices=[device],
            clusters=[ActualCluster(id="cluster-1", name="aghub", proxmox={"observer_device_id": "dev-1", "observed_at": GENERATED_AT, "observation_state": "complete", "observed_node_names": ["aghealthy"]})],
            virtual_machines=[ActualVirtualMachine(id="vm-1", name="aghealthy", cluster_id="cluster-1", vcpus=2, memory=2048, disk=20, proxmox={"guest_type": "lxc", "vmid": 101, "node": "aghealthy", "status": "running"})],
        ),
        fetched_at=datetime.now(timezone.utc),
    )


def test_valid_compute_collections_produce_drift_but_no_plan_actions():
    snapshot = _snapshot_with_valid_compute()
    context = DriftContext(generated_at=GENERATED_AT)

    result = compute_drift(snapshot, context)
    all_diffs = [diff for target in result.targets for diff in target.diffs]

    compute_targets = [target for target in result.targets if target.target.kind in ("compute_platform", "compute_instance")]
    assert {target.target.kind for target in compute_targets} == {"compute_platform", "compute_instance"}
    assert any(diff.code == "compute_instance_not_linked" for diff in all_diffs)

    plan = build_plan(
        snapshot=snapshot, diffs=all_diffs, scope=PlanScope(kind="cluster"),
        drift_generated_at=GENERATED_AT, profile_reconciliation={},
    )

    assert any(record.target.id == "instance-1" for record in plan.manual_review)
    for action in plan.actions:
        assert all(target.kind not in ("compute_platform", "compute_instance") for target in action.targets)
        assert all(target.id not in ("platform-1", "instance-1") for target in action.targets)
