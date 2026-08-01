from __future__ import annotations

from datetime import datetime, timezone

import pytest

from nctl_core.drift.workspace_evaluation import evaluate_all_workspaces, evaluate_workspace
from nctl_core.sources.actual import ActualDevice
from nctl_core.sources.desired import DesiredNode, DesiredWorkspace

NOW = datetime(2026, 8, 1, 13, 0, 0, tzinfo=timezone.utc)


def workspace(**overrides) -> DesiredWorkspace:
    base = dict(
        id="ws-1",
        slug="pj-voxel3dprint",
        name="pj-voxel3dprint",
        lifecycle="active",
        source_remote_url="https://github.com/iwaag/pj-voxel3dprint.git",
        expected_path="/home/eiji/projects/pj-voxel3dprint",
        desired_presence="present",
        node_id="node-1",
        node_slug="agpc",
    )
    base.update(overrides)
    return DesiredWorkspace(**base)


def node(realized_device_id: str | None = "dev-1") -> DesiredNode:
    return DesiredNode(
        id="node-1", slug="agpc", name="agpc", lifecycle="active", node_type="baremetal",
        realized_device_id=realized_device_id,
    )


def device(entry: dict | None) -> ActualDevice:
    facts = {"observed_workspaces": {"pj-voxel3dprint": entry}} if entry is not None else {}
    return ActualDevice(id="dev-1", name="agpc.local", facts=facts)


FRESH = "2026-08-01T12:30:00+00:00"
STALE = "2026-07-29T12:00:00+00:00"


def test_present_and_identity_matched_and_active_development():
    entry = {
        "present": True, "remote_url": "https://github.com/iwaag/pj-voxel3dprint.git",
        "ahead": 5, "behind": 0, "dirty": False, "checked_at": FRESH,
    }
    result = evaluate_workspace(workspace(), node(), {"dev-1": device(entry)}, now=NOW, stale_after_hours=24)
    assert result.gaps == []
    assert result.activity_class == "active_development"
    assert result.activity_reasons == {"ahead": 5, "behind": 0, "dirty": False}


def test_workspace_missing_when_desired_present_and_observed_absent():
    entry = {"present": False, "checked_at": FRESH}
    result = evaluate_workspace(workspace(), node(), {"dev-1": device(entry)}, now=NOW, stale_after_hours=24)
    assert {"code": "workspace_missing"} in result.gaps
    assert result.activity_class is None


def test_workspace_identity_mismatch_on_differing_remote_url():
    entry = {"present": True, "remote_url": "https://github.com/someone-else/fork.git", "checked_at": FRESH}
    result = evaluate_workspace(workspace(), node(), {"dev-1": device(entry)}, now=NOW, stale_after_hours=24)
    codes = [g["code"] for g in result.gaps]
    assert "workspace_identity_mismatch" in codes
    # still classified -- identity mismatch and activity are independent axes
    assert result.activity_class == "idle"


@pytest.mark.parametrize(
    "raw,remote_url",
    [
        ({"is_git": False}, None),
        ({}, None),
    ],
)
def test_workspace_identity_unknown_not_a_mismatch(raw, remote_url):
    entry = {"present": True, "remote_url": remote_url, "raw": raw, "checked_at": FRESH}
    result = evaluate_workspace(workspace(), node(), {"dev-1": device(entry)}, now=NOW, stale_after_hours=24)
    codes = [g["code"] for g in result.gaps]
    assert "workspace_identity_unknown" in codes
    assert "workspace_identity_mismatch" not in codes


def test_workspace_retired_present_carries_no_recommended_action():
    entry = {"present": True, "remote_url": "https://github.com/iwaag/pj-voxel3dprint.git", "checked_at": FRESH}
    result = evaluate_workspace(
        workspace(desired_presence="absent"), node(), {"dev-1": device(entry)}, now=NOW, stale_after_hours=24
    )
    matching = [g for g in result.gaps if g["code"] == "workspace_retired_present"]
    assert len(matching) == 1
    assert "recommended_actions" not in matching[0]


def test_retired_absent_with_no_observation_is_silent():
    result = evaluate_workspace(
        workspace(desired_presence="absent"), node(), {"dev-1": device(None)}, now=NOW, stale_after_hours=24
    )
    assert result.gaps == []
    assert result.observation is None


@pytest.mark.parametrize(
    "ahead,behind,dirty,expected",
    [
        (0, 0, False, "idle"),
        (None, None, True, "active_development"),
        (None, None, False, "idle"),
        (0, 3, False, "behind_origin"),
        (2, 0, False, "active_development"),
    ],
)
def test_activity_classification_edges(ahead, behind, dirty, expected):
    entry = {
        "present": True, "remote_url": "https://github.com/iwaag/pj-voxel3dprint.git",
        "ahead": ahead, "behind": behind, "dirty": dirty, "checked_at": FRESH,
    }
    result = evaluate_workspace(workspace(), node(), {"dev-1": device(entry)}, now=NOW, stale_after_hours=24)
    assert result.activity_class == expected


def test_observation_stale_beyond_threshold():
    entry = {"present": True, "remote_url": "https://github.com/iwaag/pj-voxel3dprint.git", "checked_at": STALE}
    result = evaluate_workspace(workspace(), node(), {"dev-1": device(entry)}, now=NOW, stale_after_hours=24)
    stale = [g for g in result.gaps if g["code"] == "workspace_observation_stale"]
    assert len(stale) == 1
    assert stale[0]["age_hours"] > 24


def test_observation_fresh_within_threshold_no_stale_gap():
    entry = {"present": True, "remote_url": "https://github.com/iwaag/pj-voxel3dprint.git", "checked_at": FRESH}
    result = evaluate_workspace(workspace(), node(), {"dev-1": device(entry)}, now=NOW, stale_after_hours=24)
    assert not any(g["code"] == "workspace_observation_stale" for g in result.gaps)


def test_no_realized_device_is_observation_missing():
    result = evaluate_workspace(workspace(), node(realized_device_id=None), {}, now=NOW, stale_after_hours=24)
    assert result.gaps == [{"code": "workspace_observation_missing", "reason": "no_realized_device"}]
    assert result.activity_class is None


def test_no_observation_entry_but_device_exists_is_observation_missing():
    result = evaluate_workspace(workspace(), node(), {"dev-1": device(None)}, now=NOW, stale_after_hours=24)
    assert result.gaps == [{"code": "workspace_observation_missing", "reason": "observation_missing"}]


def test_malformed_entry_degrades_to_observation_missing_not_a_crash():
    entry = {"present": "not-a-bool", "checked_at": FRESH}
    result = evaluate_workspace(workspace(), node(), {"dev-1": device(entry)}, now=NOW, stale_after_hours=24)
    codes = [g["code"] for g in result.gaps]
    assert "workspace_observation_missing" in codes
    assert result.activity_class is None


def test_ssh_and_https_remote_urls_match_both_directions():
    entry_ssh = {"present": True, "remote_url": "git@github.com:iwaag/pj-voxel3dprint.git", "checked_at": FRESH}
    result = evaluate_workspace(
        workspace(source_remote_url="https://github.com/iwaag/pj-voxel3dprint.git"),
        node(), {"dev-1": device(entry_ssh)}, now=NOW, stale_after_hours=24,
    )
    assert not any(g["code"] == "workspace_identity_mismatch" for g in result.gaps)

    entry_https = {"present": True, "remote_url": "https://github.com/iwaag/pj-voxel3dprint", "checked_at": FRESH}
    result2 = evaluate_workspace(
        workspace(source_remote_url="git@github.com:iwaag/pj-voxel3dprint.git"),
        node(), {"dev-1": device(entry_https)}, now=NOW, stale_after_hours=24,
    )
    assert not any(g["code"] == "workspace_identity_mismatch" for g in result2.gaps)


def test_no_informational_status_string_ever_appears_as_a_gap_code():
    """Exit criterion 3 regression guard: activity classes must never leak into `gaps`."""
    from nctl_core.drift.workspace_evaluation import ACTIVITY_CLASSES

    scenarios = [
        {"present": True, "remote_url": "https://github.com/iwaag/pj-voxel3dprint.git", "ahead": 5, "checked_at": FRESH},
        {"present": True, "remote_url": "https://github.com/iwaag/pj-voxel3dprint.git", "behind": 3, "checked_at": FRESH},
        {"present": True, "remote_url": "https://github.com/iwaag/pj-voxel3dprint.git", "checked_at": FRESH},
        {"present": False, "checked_at": FRESH},
        {"present": True, "remote_url": "mismatch", "checked_at": FRESH},
    ]
    for entry in scenarios:
        result = evaluate_workspace(workspace(), node(), {"dev-1": device(entry)}, now=NOW, stale_after_hours=24)
        codes = {g["code"] for g in result.gaps}
        assert not codes & set(ACTIVITY_CLASSES)


def test_evaluate_all_workspaces_keys_by_workspace_id():
    entry = {"present": True, "remote_url": "https://github.com/iwaag/pj-voxel3dprint.git", "checked_at": FRESH}
    result = evaluate_all_workspaces(
        [workspace()], [node()], [device(entry)], now=NOW, stale_after_hours=24
    )
    assert set(result) == {"ws-1"}
    assert result["ws-1"].gaps == []
