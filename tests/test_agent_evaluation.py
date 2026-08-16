"""Registration is drift, liveness is not: the agent evaluation's two lanes."""

from __future__ import annotations

from datetime import datetime, timezone

from nctl_core.drift.agent_evaluation import evaluate_all_agents
from nctl_core.sources.actual import ActualDevice, ObservedAgentRegistration
from nctl_core.sources.desired import DesiredAgent, DesiredNode, DesiredWorkspace

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


def agent(**overrides) -> DesiredAgent:
    values = {
        "id": "ag-1", "slug": "agforge", "name": "agforge", "lifecycle": "active",
        "zulip_user_id": 13, "plane_user_id": "plane-13",
        "desired_zulip_channels": ["FreeForge", "ops"],
        "workspace_id": "ws-1",
    }
    values.update(overrides)
    return DesiredAgent(**values)


def registration(**overrides) -> ObservedAgentRegistration:
    values = {
        "id": "obs-1", "agent_id": "ag-1", "agent_slug": "agforge",
        "observed_at": "2026-08-17T11:59:00+00:00",
        "zulip_present": True, "zulip_user_id": 13, "zulip_is_active": True,
        "zulip_channels": ["FreeForge", "ops"],
        "plane_present": True, "plane_user_id": "plane-13", "plane_role": 20,
    }
    values.update(overrides)
    return ObservedAgentRegistration(**values)


def workspace(**overrides) -> DesiredWorkspace:
    values = {
        "id": "ws-1", "slug": "agforge", "name": "agforge", "lifecycle": "active",
        "source_remote_url": "https://github.com/iwaag/agforge.git",
        "expected_path": "/w/agforge", "desired_presence": "present",
        "node_id": "node-1", "node_slug": "agstudio",
    }
    values.update(overrides)
    return DesiredWorkspace(**values)


def node() -> DesiredNode:
    return DesiredNode(
        id="node-1", slug="agstudio", name="agstudio", lifecycle="active",
        node_type="device", realized_device_id="dev-1",
    )


def device(agent_status, *, workspace_slug="agforge") -> ActualDevice:
    entry = {"present": True}
    if agent_status is not None:
        entry["agent_status"] = agent_status
    return ActualDevice(
        id="dev-1", name="agstudio",
        facts={"observed_workspaces": {workspace_slug: entry}},
    )


def polling_status(age_seconds=30.0, seconds_since_collection=10.0, **overrides):
    checked_at = NOW.timestamp() - seconds_since_collection
    status = {
        "present": True, "readable": True, "age_seconds": age_seconds,
        "checked_at": datetime.fromtimestamp(checked_at, timezone.utc).isoformat(),
        "queue_id": "queue-1", "last_error": None,
    }
    status.update(overrides)
    return status


def evaluate(agents=None, workspaces=None, devices=None, registrations=None, **kwargs):
    return evaluate_all_agents(
        agents if agents is not None else [agent()],
        workspaces if workspaces is not None else [workspace()],
        [node()],
        devices if devices is not None else [device(polling_status())],
        registrations if registrations is not None else [registration()],
        now=NOW,
        **kwargs,
    )["ag-1"]


def test_a_fully_registered_polling_agent_has_no_gaps():
    result = evaluate()
    assert result.gaps == []
    assert result.liveness_class == "polling"
    assert result.liveness_reasons["effective_age_seconds"] == 40.0


def test_a_missing_zulip_account_is_a_gap():
    result = evaluate(registrations=[registration(zulip_present=False, zulip_channels=[])])
    assert [gap["code"] for gap in result.gaps] == ["agent_zulip_account_missing"]


def test_a_deactivated_zulip_account_is_a_gap_distinct_from_a_missing_one():
    result = evaluate(registrations=[registration(zulip_is_active=False)])
    assert [gap["code"] for gap in result.gaps] == ["agent_zulip_account_deactivated"]


def test_an_unsubscribed_desired_channel_is_a_gap_naming_the_channels():
    result = evaluate(registrations=[registration(zulip_channels=["ops"])])
    assert result.gaps == [{"code": "agent_zulip_channel_unsubscribed", "channels": ["FreeForge"]}]


def test_extra_subscriptions_beyond_the_desired_set_are_not_drift():
    result = evaluate(registrations=[registration(zulip_channels=["FreeForge", "ops", "sandbox"])])
    assert result.gaps == []


def test_a_missing_plane_membership_is_a_gap():
    result = evaluate(registrations=[registration(plane_present=False)])
    assert [gap["code"] for gap in result.gaps] == ["agent_plane_membership_missing"]


def test_an_agent_with_no_declared_ids_reports_undeclared_not_missing():
    result = evaluate(
        agents=[agent(zulip_user_id=None, plane_user_id="", desired_zulip_channels=[])],
        registrations=[registration(zulip_present=False, plane_present=False)],
    )
    assert [gap["code"] for gap in result.gaps] == [
        "agent_zulip_identity_undeclared", "agent_plane_identity_undeclared",
    ]


def test_a_never_observed_agent_says_so_instead_of_claiming_every_gap():
    result = evaluate(registrations=[])
    assert [gap["code"] for gap in result.gaps] == ["agent_registration_unobserved"]


def test_a_non_active_agent_is_not_evaluated_for_registration():
    result = evaluate(agents=[agent(lifecycle="retired")], registrations=[])
    assert result.gaps == []


def test_liveness_goes_stale_past_the_threshold_without_producing_a_gap():
    result = evaluate(devices=[device(polling_status(age_seconds=400.0))])
    assert result.liveness_class == "stale"
    assert result.gaps == []


def test_the_threshold_is_inclusive_at_exactly_three_poll_windows():
    result = evaluate(devices=[device(polling_status(age_seconds=260.0, seconds_since_collection=10.0))])
    assert result.liveness_class == "polling"
    result = evaluate(devices=[device(polling_status(age_seconds=261.0, seconds_since_collection=10.0))])
    assert result.liveness_class == "stale"


def test_collection_age_counts_toward_liveness_so_a_frozen_observation_goes_stale():
    """A status file that was fresh when collected, but collected long ago, is not liveness."""
    result = evaluate(devices=[device(polling_status(age_seconds=5.0, seconds_since_collection=3600.0))])
    assert result.liveness_class == "stale"


def test_every_way_of_not_knowing_is_unobserved_with_a_reason():
    cases = {
        "no_desired_workspace": dict(agents=[agent(workspace_id=None)]),
        "workspace_unobserved": dict(devices=[device(polling_status(), workspace_slug="other")]),
        "no_status_file": dict(devices=[device(None)]),
        "status_file_unreadable": dict(devices=[device({"present": True, "readable": False})]),
        "status_age_missing": dict(devices=[device(polling_status(age_seconds="soon"))]),
        "no_realized_device": dict(devices=[]),
    }
    for reason, kwargs in cases.items():
        result = evaluate(**kwargs)
        assert result.liveness_class == "unobserved", reason
        assert result.liveness_reasons["reason"] == reason
        assert result.gaps == [], "liveness must never produce a gap"


def test_a_recorded_listener_error_travels_with_the_liveness_reasons():
    result = evaluate(devices=[device(polling_status(last_error="queue expired"))])
    assert result.liveness_class == "polling"
    assert result.liveness_reasons["last_error"] == "queue expired"
