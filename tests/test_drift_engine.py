from __future__ import annotations

from datetime import datetime, timezone

from nctl_core.drift.context import DriftContext
from nctl_core.drift.engine import compute_drift
from nctl_core.drift.model import Status
from nctl_core.sources.actual import ActualDevice, ActualSnapshot
from nctl_core.sources.desired import DesiredNode, DesiredNodeOperationalConfig, DesiredServicePlacement, DesiredSnapshot
from nctl_core.sources.snapshot import SourceSnapshot


def make_snapshot(*, nodes=(), operational_configs=(), placements=(), devices=(), observed=()) -> SourceSnapshot:
    return SourceSnapshot(
        desired=DesiredSnapshot(nodes=list(nodes), operational_configs=list(operational_configs), placements=list(placements)),
        actual=ActualSnapshot(devices=list(devices)),
        observed=list(observed),
        fetched_at=datetime.now(timezone.utc),
    )


def test_node_with_no_diffs_is_seeded_as_converged():
    # Step 4's evaluation-port comparators (node_intent_matching) flag any
    # unlinked node with no actual-node candidate as `missing_actual_node`,
    # so a genuinely gap-free node needs a realized device that resolves
    # cleanly (matching nintent's real Evaluate Node Intent Job behavior).
    node = DesiredNode(id="n1", slug="agok", name="agok", lifecycle="active", node_type="device", realized_device_id="dev-1")
    device = ActualDevice(id="dev-1", name="agok.local")
    snapshot = make_snapshot(nodes=[node], devices=[device])
    context = DriftContext(generated_at="2026-07-15T12:00:00+00:00")

    result = compute_drift(snapshot, context)

    [target_status] = result.targets
    assert target_status.target.slug == "agok"
    assert target_status.status == Status.CONVERGED
    assert target_status.diffs == []
    assert result.summary["converged"] == 1


def test_node_missing_realized_device_is_unknown():
    node = DesiredNode(id="n1", slug="agmissing", name="agmissing", lifecycle="active", node_type="device", realized_device_id="dev-gone")
    snapshot = make_snapshot(nodes=[node])
    context = DriftContext(generated_at="2026-07-15T12:00:00+00:00")

    result = compute_drift(snapshot, context)

    [target_status] = result.targets
    assert target_status.status == Status.UNKNOWN
    assert result.summary["unknown"] == 1


def test_multiple_nodes_sorted_and_summarized_independently():
    # Targets sort by (kind, id) — id is a node's primary identity, so this
    # asserts on id order (n1 before n2), not slug alphabetical order.
    ok_node = DesiredNode(id="n1", slug="agok", name="agok", lifecycle="active", node_type="device", realized_device_id="dev-1")
    bad_node = DesiredNode(id="n2", slug="agbad", name="agbad", lifecycle="active", node_type="device", realized_device_id="dev-gone")
    device = ActualDevice(id="dev-1", name="agok.local")
    snapshot = make_snapshot(nodes=[bad_node, ok_node], devices=[device])
    context = DriftContext(generated_at="2026-07-15T12:00:00+00:00")

    result = compute_drift(snapshot, context)

    assert [t.target.id for t in result.targets] == ["n1", "n2"]
    assert result.summary == {"converged": 1, "drifting": 0, "converging": 0, "unknown": 1}


def test_global_diff_from_production_policy_appears_as_its_own_target():
    # A malformed shared deployment-profile map (Group A) is the only
    # composition failure that still produces a `global` target after Phase
    # 1 -- every node/placement-owned Group C code (e.g. invalid_platform_power)
    # is node-local instead, per p0/field-classification.md Section 6.
    node = DesiredNode(id="n1", slug="agbad", name="agbad", lifecycle="active", node_type="device", realized_device_id="dev-1")
    op_config = DesiredNodeOperationalConfig(
        id="op1",
        node_id="n1",
        actual_state_policy="required",
        connection_path="local",
        expected_host_os="linux",
    )
    device = ActualDevice(id="dev-1", name="agbad.local", facts={"host_system": "Linux", "last_seen": "2026-07-15T11:00:00+00:00"})
    snapshot = make_snapshot(nodes=[node], operational_configs=[op_config], devices=[device])
    profiles = {"web": {"group": "web_server", "config_schema_version": "1", "variables": "not-an-object"}}
    context = DriftContext(generated_at="2026-07-15T12:00:00+00:00", profiles=profiles)

    result = compute_drift(snapshot, context)

    kinds = {t.target.kind for t in result.targets}
    assert "global" in kinds
    global_target = next(t for t in result.targets if t.target.kind == "global")
    assert global_target.status == Status.DRIFTING
    assert global_target.diffs[0].code == "invalid_profile_variables"


# --- Step 1.6: mixed good/bad production composition through the real engine


def test_mixed_snapshot_isolates_local_error_and_unapplied_intent_through_the_engine():
    # A full compute_drift() pass, not just production_policy() directly:
    # one node has a Group C local error (unknown_profile), one node has an
    # active placement recorded on a lifecycle-ineligible node, and a third
    # node is fully healthy. None of this may create a global target, and
    # the healthy node's status/diffs must be evaluated independently.
    healthy = DesiredNode(id="n1", slug="aghealthy", name="aghealthy", lifecycle="active", node_type="device", realized_device_id="dev-1")
    bad = DesiredNode(id="n2", slug="agbad", name="agbad", lifecycle="active", node_type="device", realized_device_id="dev-2")
    planned = DesiredNode(id="n3", slug="agplanned", name="agplanned", lifecycle="planned", node_type="device", realized_device_id="dev-3")

    healthy_op = DesiredNodeOperationalConfig(
        id="op1", node_id="n1", actual_state_policy="required", connection_path="local", expected_host_os="linux"
    )
    bad_op = DesiredNodeOperationalConfig(
        id="op2", node_id="n2", actual_state_policy="required", connection_path="local", expected_host_os="linux"
    )
    planned_op = DesiredNodeOperationalConfig(
        id="op3", node_id="n3", actual_state_policy="required", connection_path="local", expected_host_os="linux"
    )
    bad_placement = DesiredServicePlacement(
        id="p1", service_id="s1", node_id="n2", instance_name="primary",
        deployment_profile="missing-profile", config_schema_version="1", config={},
    )
    planned_placement = DesiredServicePlacement(
        id="p2", service_id="s2", node_id="n3", instance_name="primary",
        deployment_profile="web", config_schema_version="1", config={"enabled": True},
    )
    device1 = ActualDevice(id="dev-1", name="aghealthy.local", facts={"host_system": "Linux", "last_seen": "2026-07-15T11:00:00+00:00"})
    device2 = ActualDevice(id="dev-2", name="agbad.local", facts={"host_system": "Linux", "last_seen": "2026-07-15T11:00:00+00:00"})
    device3 = ActualDevice(id="dev-3", name="agplanned.local", facts={"host_system": "Linux", "last_seen": "2026-07-15T11:00:00+00:00"})
    snapshot = make_snapshot(
        nodes=[healthy, bad, planned],
        operational_configs=[healthy_op, bad_op, planned_op],
        placements=[bad_placement, planned_placement],
        devices=[device1, device2, device3],
    )
    profiles = {"web": {"group": "web_server", "config_schema_version": "1", "variables": {}}}
    context = DriftContext(generated_at="2026-07-15T12:00:00+00:00", profiles=profiles)

    result = compute_drift(snapshot, context)

    assert "global" not in {t.target.kind for t in result.targets}

    healthy_status = next(t for t in result.targets if t.target.slug == "aghealthy")
    bad_status = next(t for t in result.targets if t.target.slug == "agbad")
    planned_status = next(t for t in result.targets if t.target.slug == "agplanned")

    assert healthy_status.status == Status.CONVERGED
    assert [d.code for d in bad_status.diffs if d.code == "unknown_profile"]
    assert bad_status.status == Status.DRIFTING

    unapplied = [d for d in planned_status.diffs if d.code == "active_placement_not_applied"]
    assert len(unapplied) == 1
    assert unapplied[0].severity.value == "warning"
    assert unapplied[0].desired["placement"]["config"] == {"enabled": True}
    # A warning-only finding keeps the target converged (Decision 4).
    assert planned_status.status == Status.CONVERGED


def test_active_placement_not_applied_not_duplicated_when_profiles_are_present():
    node = DesiredNode(id="n1", slug="agplanned", name="agplanned", lifecycle="deprecated", node_type="device")
    placement = DesiredServicePlacement(
        id="p1", service_id="s1", node_id="n1", instance_name="primary",
        deployment_profile="web", config_schema_version="1", config={},
    )
    snapshot = make_snapshot(nodes=[node], placements=[placement])
    profiles = {"web": {"group": "web_server", "config_schema_version": "1", "variables": {}}}
    context = DriftContext(generated_at="2026-07-15T12:00:00+00:00", profiles=profiles)

    result = compute_drift(snapshot, context)

    [target_status] = result.targets
    unapplied = [d for d in target_status.diffs if d.code == "active_placement_not_applied"]
    assert len(unapplied) == 1
