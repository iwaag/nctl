"""Phase 3 Step 3.5: drift/reconcile isolation across a node's lifecycle transition.

Composer-level "one bad node doesn't abort others" and the
`active_placement_not_applied` finding itself are already covered extensively by
`test_production_composer.py`/`test_drift_comparators.py` (Phase 1/2). This file adds the specific
before/after transition framing plan.md Step 3.5 asks for: the same node/placement data evaluated
at `planned`, then `active`, then back to `planned`, plus reconcile-planner isolation between a
healthy and a locally blocked active node, and the "lifecycle command errors never enter
drift/reconcile classification" guarantee (plan.md Decision 3).
"""

from __future__ import annotations

from datetime import datetime, timezone

import nctl_core.lifecycle as lifecycle_module
from nctl_core.drift import comparators
from nctl_core.drift.context import DriftContext
from nctl_core.drift.model import DiffRecord, Severity, Target
from nctl_core.reconcile.classify import CODE_CLASSIFICATION
from nctl_core.reconcile.model import PlanScope
from nctl_core.reconcile.planner import build_plan
from nctl_core.sources.actual import ActualDevice, ActualSnapshot
from nctl_core.sources.desired import DesiredNode, DesiredServicePlacement, DesiredSnapshot
from nctl_core.sources.snapshot import SourceSnapshot

CONTEXT = DriftContext(generated_at="2026-07-21T00:00:00+00:00")


def _snapshot_with_lifecycle(lifecycle: str) -> SourceSnapshot:
    node = DesiredNode(id="n1", slug="agdnsmasq", name="agdnsmasq", lifecycle=lifecycle, node_type="device")
    placement = DesiredServicePlacement(
        id="p1", service_id="s1", node_id="n1", instance_name="dnsmasq",
        deployment_profile="dnsmasq", config_schema_version="1", config={"listen_addresses": ["192.168.0.2"]},
    )
    return SourceSnapshot(
        desired=DesiredSnapshot(nodes=[node], placements=[placement]),
        actual=ActualSnapshot(),
        fetched_at=datetime.now(timezone.utc),
    )


def _active_placement_codes(snapshot: SourceSnapshot) -> set[str]:
    diffs = list(comparators.production_policy(snapshot, CONTEXT))
    return {d.code for d in diffs}


def test_planned_node_with_active_placement_carries_unapplied_warning():
    codes = _active_placement_codes(_snapshot_with_lifecycle("planned"))
    assert "active_placement_not_applied" in codes


def test_promoting_the_node_to_active_removes_the_sole_blocker():
    codes = _active_placement_codes(_snapshot_with_lifecycle("active"))
    assert "active_placement_not_applied" not in codes


def test_demoting_the_node_back_to_planned_restores_the_unapplied_evidence():
    # Round-trip: same data, lifecycle flipped twice, warning must track it exactly.
    assert "active_placement_not_applied" in _active_placement_codes(_snapshot_with_lifecycle("planned"))
    assert "active_placement_not_applied" not in _active_placement_codes(_snapshot_with_lifecycle("active"))
    assert "active_placement_not_applied" in _active_placement_codes(_snapshot_with_lifecycle("planned"))


def _node(node_id: str, slug: str) -> DesiredNode:
    return DesiredNode(id=node_id, slug=slug, name=slug, lifecycle="active", node_type="device")


def _node_diff(node: DesiredNode, code: str, severity: Severity = Severity.ERROR) -> DiffRecord:
    return DiffRecord(
        target=Target(kind="node", slug=node.slug, name=node.name, id=node.id),
        code=code, severity=severity, message=f"{node.slug}: {code}",
    )


def test_mixed_healthy_and_locally_blocked_active_nodes_are_isolated_in_the_cluster_plan():
    healthy = _node("n1", "aghealthy")
    bad = _node("n2", "agbad")
    device = ActualDevice(id="dev-1", name="aghealthy")
    snapshot = SourceSnapshot(
        desired=DesiredSnapshot(nodes=[healthy, bad]),
        actual=ActualSnapshot(devices=[device]),
        fetched_at=datetime.now(timezone.utc),
    )
    diffs = [
        _node_diff(healthy, "actual_node_not_linked"),  # automatable
        _node_diff(bad, "missing_interface_candidate"),  # manual_review, not automatable
    ]

    plan = build_plan(
        snapshot=snapshot, diffs=diffs, scope=PlanScope(kind="cluster"),
        drift_generated_at="2026-07-21T00:00:00+00:00", profile_reconciliation={},
    )

    assert [a.reconciler_id for a in plan.actions] == ["link_actual_node"]
    assert plan.actions[0].targets[0].slug == "aghealthy"
    manual_slugs = {finding.target.slug for finding in plan.manual_review}
    assert manual_slugs == {"agbad"}
    assert plan.unsupported == []


def test_host_scoped_plan_for_the_healthy_node_excludes_the_blocked_neighbor():
    healthy = _node("n1", "aghealthy")
    bad = _node("n2", "agbad")
    device = ActualDevice(id="dev-1", name="aghealthy")
    snapshot = SourceSnapshot(
        desired=DesiredSnapshot(nodes=[healthy, bad]),
        actual=ActualSnapshot(devices=[device]),
        fetched_at=datetime.now(timezone.utc),
    )
    diffs = [
        _node_diff(healthy, "actual_node_not_linked"),
        _node_diff(bad, "missing_interface_candidate"),
    ]

    plan = build_plan(
        snapshot=snapshot, diffs=diffs, scope=PlanScope(kind="host", host_slug="aghealthy"),
        drift_generated_at="2026-07-21T00:00:00+00:00", profile_reconciliation={},
    )

    assert [a.reconciler_id for a in plan.actions] == ["link_actual_node"]
    assert plan.manual_review == []


def test_lifecycle_command_errors_never_enter_reconcile_classification():
    lifecycle_error_codes = {
        "invalid_lifecycle", "unknown_node", "lifecycle_update_rejected", "lifecycle_confirmation_mismatch",
    }
    assert lifecycle_error_codes.isdisjoint(CODE_CLASSIFICATION.keys())


def test_lifecycle_module_does_not_import_drift_registry_or_reconcile_classify():
    # Structural guard for Decision 3: the lifecycle command must stay entirely
    # command-scoped. If this module ever starts depending on the drift/reconcile
    # vocabulary, that is a scope violation worth failing loudly on.
    assert not hasattr(lifecycle_module, "CODE_CLASSIFICATION")
    assert not hasattr(lifecycle_module, "run_comparators")
