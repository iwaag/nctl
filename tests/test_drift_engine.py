from __future__ import annotations

from datetime import datetime, timezone

from nctl_core.drift.context import DriftContext
from nctl_core.drift.engine import compute_drift
from nctl_core.drift.model import Status
from nctl_core.sources.actual import ActualDevice, ActualSnapshot
from nctl_core.sources.desired import DesiredNode, DesiredNodeOperationalConfig, DesiredSnapshot
from nctl_core.sources.snapshot import SourceSnapshot


def make_snapshot(*, nodes=(), operational_configs=(), devices=(), observed=()) -> SourceSnapshot:
    return SourceSnapshot(
        desired=DesiredSnapshot(nodes=list(nodes), operational_configs=list(operational_configs)),
        actual=ActualSnapshot(devices=list(devices)),
        observed=list(observed),
        fetched_at=datetime.now(timezone.utc),
    )


def test_node_with_no_diffs_is_seeded_as_converged():
    node = DesiredNode(id="n1", slug="agok", name="agok", lifecycle="active", node_type="device")
    snapshot = make_snapshot(nodes=[node])
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
    ok_node = DesiredNode(id="n1", slug="agok", name="agok", lifecycle="active", node_type="device")
    bad_node = DesiredNode(id="n2", slug="agbad", name="agbad", lifecycle="active", node_type="device", realized_device_id="dev-gone")
    snapshot = make_snapshot(nodes=[bad_node, ok_node])
    context = DriftContext(generated_at="2026-07-15T12:00:00+00:00")

    result = compute_drift(snapshot, context)

    assert [t.target.id for t in result.targets] == ["n1", "n2"]
    assert result.summary == {"converged": 1, "drifting": 0, "converging": 0, "unknown": 1}


def test_global_diff_from_production_policy_appears_as_its_own_target():
    node = DesiredNode(id="n1", slug="agbad", name="agbad", lifecycle="active", node_type="device", realized_device_id="dev-1")
    op_config = DesiredNodeOperationalConfig(
        id="op1",
        node_id="n1",
        actual_state_policy="required",
        connection_path="local",
        expected_host_os="linux",
        power_control="macos_sleep",
    )
    device = ActualDevice(id="dev-1", name="agbad.local", facts={"host_system": "Linux", "last_seen": "2026-07-15T11:00:00+00:00"})
    snapshot = make_snapshot(nodes=[node], operational_configs=[op_config], devices=[device])
    profiles = {"web": {"group": "web_server", "config_schema_version": "1", "variables": {}}}
    context = DriftContext(generated_at="2026-07-15T12:00:00+00:00", profiles=profiles)

    result = compute_drift(snapshot, context)

    kinds = {t.target.kind for t in result.targets}
    assert "global" in kinds
    global_target = next(t for t in result.targets if t.target.kind == "global")
    assert global_target.status == Status.DRIFTING
    assert global_target.diffs[0].code == "invalid_platform_power"
