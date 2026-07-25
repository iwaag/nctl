"""VM Phase 3 Step 5 point 5/7: valid compute collections must stay out of compute
drift/planner/reconcile dispatch entirely -- Step 5 only reads/types/
validates `DesiredComputePlatform`/`DesiredComputeInstance`, it adds no comparator,
reconciler, or plan action that even looks at them. Compute drift/matching/link
planning is explicitly Phase 4 territory (plan.md Section 5.1).

This test runs the *real* `run_comparators()`/`build_plan()` pipeline (not mocks)
over a snapshot that includes a fully valid platform+instance and asserts zero
diffs/actions/manual-review/unsupported records reference them.
"""

from __future__ import annotations

from datetime import datetime, timezone

from nctl_core.drift.context import DriftContext
from nctl_core.drift.engine import compute_drift
from nctl_core.reconcile.model import PlanScope
from nctl_core.reconcile.planner import build_plan
from nctl_core.sources.actual import ActualDevice, ActualSnapshot
from nctl_core.sources.desired import DesiredComputeInstance, DesiredComputePlatform, DesiredNode, DesiredSnapshot
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
        actual=ActualSnapshot(devices=[device]),
        fetched_at=datetime.now(timezone.utc),
    )


def test_valid_compute_collections_produce_no_drift_and_no_plan_actions():
    snapshot = _snapshot_with_valid_compute()
    context = DriftContext(generated_at=GENERATED_AT)

    result = compute_drift(snapshot, context)
    all_diffs = [diff for target in result.targets for diff in target.diffs]

    assert all(target.target.kind not in ("compute_platform", "compute_instance") for target in result.targets)
    assert all(diff.target.kind not in ("compute_platform", "compute_instance") for diff in all_diffs)
    assert all("compute" not in diff.code for diff in all_diffs)
    assert not any(diff.target.id in ("platform-1", "instance-1") for diff in all_diffs)

    plan = build_plan(
        snapshot=snapshot, diffs=all_diffs, scope=PlanScope(kind="cluster"),
        drift_generated_at=GENERATED_AT, profile_reconciliation={},
    )

    for record in (*plan.manual_review, *plan.unsupported):
        assert record.target.kind not in ("compute_platform", "compute_instance")
        assert record.target.id not in ("platform-1", "instance-1")
    for action in plan.actions:
        assert all(target.kind not in ("compute_platform", "compute_instance") for target in action.targets)
        assert all(target.id not in ("platform-1", "instance-1") for target in action.targets)
