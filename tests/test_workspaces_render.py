"""Pure-projection tests for `render_workspaces_data` (creative_workspace p2 Step 3).

Constructs a `SourceSnapshot` directly (no GraphQL mocking) -- the projection
never touches Nautobot itself, mirroring `test_relations_render.py`.
"""

from __future__ import annotations

from datetime import datetime, timezone

from nctl_core.sources.actual import ActualDevice, ActualSnapshot
from nctl_core.sources.desired import DesiredNode, DesiredSnapshot, DesiredWorkspace
from nctl_core.sources.snapshot import SourceSnapshot
from nctl_core.workspaces_render import render_workspaces_data

GENERATED_AT = "2026-08-01T13:00:00+00:00"
FRESH = "2026-08-01T12:30:00+00:00"


def _workspace(**overrides) -> DesiredWorkspace:
    base = dict(
        id="ws-1", slug="pj-voxel3dprint", name="pj-voxel3dprint", lifecycle="active",
        source_remote_url="https://github.com/iwaag/pj-voxel3dprint.git",
        expected_path="/home/eiji/projects/pj-voxel3dprint",
        desired_presence="present", node_id="node-1", node_slug="agpc",
    )
    base.update(overrides)
    return DesiredWorkspace(**base)


def _node(realized_device_id: str | None = "dev-1") -> DesiredNode:
    return DesiredNode(
        id="node-1", slug="agpc", name="agpc", lifecycle="active", node_type="baremetal",
        realized_device_id=realized_device_id,
    )


def _snapshot(workspace: DesiredWorkspace, node: DesiredNode, entry: dict | None) -> SourceSnapshot:
    facts = {"observed_workspaces": {workspace.slug: entry}} if entry is not None else {}
    device = ActualDevice(id="dev-1", name="agpc.local", facts=facts)
    return SourceSnapshot(
        desired=DesiredSnapshot(nodes=[node], workspaces=[workspace]),
        actual=ActualSnapshot(devices=[device]),
        fetched_at=datetime.now(timezone.utc),
    )


def test_row_reports_present_matched_and_activity_class():
    entry = {
        "present": True, "remote_url": "https://github.com/iwaag/pj-voxel3dprint.git",
        "ahead": 5, "behind": 0, "dirty": False, "checked_at": FRESH,
    }
    snapshot = _snapshot(_workspace(), _node(), entry)

    data = render_workspaces_data(snapshot, GENERATED_AT)

    assert len(data.rows) == 1
    row = data.rows[0]
    assert row.slug == "pj-voxel3dprint"
    assert row.node == "agpc"
    assert row.presence == "present"
    assert row.identity == "matched"
    assert row.activity_class == "active_development"
    assert row.activity_reasons == {"ahead": 5, "behind": 0, "dirty": False}
    assert row.freshness == "fresh"
    assert row.checked_at == FRESH
    assert row.gap_codes == []
    assert data.summary == {"converged": 1}


def test_row_reports_missing_presence_and_no_activity():
    entry = {"present": False, "checked_at": FRESH}
    snapshot = _snapshot(_workspace(), _node(), entry)

    data = render_workspaces_data(snapshot, GENERATED_AT)

    row = data.rows[0]
    assert row.presence == "missing"
    assert row.gap_codes == ["workspace_missing"]
    assert row.activity_class is None
    assert data.summary == {"drifting": 1}


def test_row_reports_identity_unknown_with_reason_for_non_git_checkout():
    entry = {"present": True, "raw": {"is_git": False}, "checked_at": FRESH}
    snapshot = _snapshot(_workspace(), _node(), entry)

    data = render_workspaces_data(snapshot, GENERATED_AT)

    row = data.rows[0]
    assert row.identity == "unknown"
    assert row.identity_reason == "not_a_git_repository"
    assert "workspace_identity_mismatch" not in row.gap_codes


def test_row_reports_stale_freshness_beyond_threshold():
    entry = {"present": True, "remote_url": "https://github.com/iwaag/pj-voxel3dprint.git", "checked_at": "2026-07-29T12:00:00+00:00"}
    snapshot = _snapshot(_workspace(), _node(), entry)

    data = render_workspaces_data(snapshot, GENERATED_AT, stale_after_hours=24)

    row = data.rows[0]
    assert row.freshness == "stale"
    assert "workspace_observation_stale" in row.gap_codes


def test_row_not_observed_for_retired_absent_with_no_entry():
    snapshot = _snapshot(_workspace(desired_presence="absent"), _node(), None)

    data = render_workspaces_data(snapshot, GENERATED_AT)

    row = data.rows[0]
    assert row.freshness == "not_observed"
    assert row.gap_codes == []
    assert data.summary == {"converged": 1}


def test_host_filter():
    workspace_a = _workspace(id="ws-a", slug="ws-a", node_id="node-1", node_slug="agpc")
    workspace_b = _workspace(id="ws-b", slug="ws-b", node_id="node-2", node_slug="agstudio")
    node_a = _node()
    node_b = DesiredNode(id="node-2", slug="agstudio", name="agstudio", lifecycle="active", node_type="baremetal", realized_device_id=None)
    snapshot = SourceSnapshot(
        desired=DesiredSnapshot(nodes=[node_a, node_b], workspaces=[workspace_a, workspace_b]),
        actual=ActualSnapshot(devices=[]),
        fetched_at=datetime.now(timezone.utc),
    )

    data = render_workspaces_data(snapshot, GENERATED_AT, host="agpc")

    assert [row.slug for row in data.rows] == ["ws-a"]


def test_rows_sorted_by_slug():
    workspace_b = _workspace(id="ws-b", slug="zeta", node_id="node-1", node_slug="agpc")
    workspace_a = _workspace(id="ws-a", slug="alpha", node_id="node-1", node_slug="agpc")
    node = _node()
    snapshot = SourceSnapshot(
        desired=DesiredSnapshot(nodes=[node], workspaces=[workspace_b, workspace_a]),
        actual=ActualSnapshot(devices=[]),
        fetched_at=datetime.now(timezone.utc),
    )

    data = render_workspaces_data(snapshot, GENERATED_AT)

    assert [row.slug for row in data.rows] == ["alpha", "zeta"]


def test_no_workspaces_declared_yields_empty_rows_and_no_summary():
    snapshot = SourceSnapshot(
        desired=DesiredSnapshot(nodes=[]), actual=ActualSnapshot(devices=[]), fetched_at=datetime.now(timezone.utc),
    )

    data = render_workspaces_data(snapshot, GENERATED_AT)

    assert data.rows == []
    assert data.summary == {}


def test_render_workspaces_text_shows_identity_unknown_reason():
    from nctl_core.output import Envelope
    from nctl_core.workspaces_render import WorkspaceRow, WorkspacesData, render_workspaces_text

    data = WorkspacesData(
        generated_at=GENERATED_AT,
        rows=[
            WorkspaceRow(
                slug="pj-voxel3dprint", name="pj-voxel3dprint", node="agpc",
                desired_presence="present", presence="present", identity="unknown",
                identity_reason="not_a_git_repository", activity_class=None, freshness="fresh",
                checked_at=FRESH, gap_codes=["workspace_identity_unknown"],
            ),
        ],
        summary={"drifting": 1},
    )
    text = render_workspaces_text(Envelope.build("nctl.workspaces.v1", data, []))
    assert "identity=unknown (not a git repo)" in text
    assert "gaps: workspace_identity_unknown" in text


def test_render_workspaces_text_no_rows():
    from nctl_core.output import Envelope
    from nctl_core.workspaces_render import WorkspacesData, render_workspaces_text

    text = render_workspaces_text(Envelope.build("nctl.workspaces.v1", WorkspacesData(), []))
    assert "(no declared workspaces)" in text
