"""Collector unit tests: what the Zulip/Plane reads become, with no live realm."""

from __future__ import annotations

from datetime import datetime, timezone

from nctl_core.agent_registration import (
    PAYLOAD_SCHEMA,
    AgentRegistrationCollection,
    collect_agent_registration,
)
from nctl_core.sources.desired import DesiredAgent

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


class FakeZulip:
    def __init__(self, members, streams, subscriptions):
        self._members = members
        self._streams = streams
        self._subscriptions = subscriptions
        self.subscription_queries = []

    def users(self):
        return self._members

    def stream_ids(self):
        return self._streams

    def is_subscribed(self, user_id, stream_id):
        self.subscription_queries.append((user_id, stream_id))
        return (user_id, stream_id) in self._subscriptions


class FakePlane:
    def __init__(self, members):
        self._members = members

    def members(self):
        return self._members


def agent(**overrides) -> DesiredAgent:
    values = {
        "id": "ag-1", "slug": "agforge", "name": "agforge", "lifecycle": "active",
        "zulip_user_id": 13, "plane_user_id": "plane-13",
        "desired_zulip_channels": ["FreeForge", "ops"],
    }
    values.update(overrides)
    return DesiredAgent(**values)


def zulip(**overrides) -> FakeZulip:
    values = {
        "members": {13: {"user_id": 13, "is_active": True}},
        "streams": {"FreeForge": 5, "ops": 4, "general": 3},
        "subscriptions": {(13, 5), (13, 4)},
    }
    values.update(overrides)
    return FakeZulip(values["members"], values["streams"], values["subscriptions"])


def test_fully_registered_agent_reports_every_desired_channel():
    result = collect_agent_registration(
        [agent()], zulip(), FakePlane({"plane-13": {"id": "plane-13", "role": 20}}), now=NOW
    )
    row = result.agents[0]
    assert row.zulip_present is True
    assert row.zulip_is_active is True
    assert row.zulip_channels == ["FreeForge", "ops"]
    assert row.plane_present is True
    assert row.plane_role == 20
    assert result.observed_at == NOW


def test_missing_subscription_is_simply_absent_from_observed_channels():
    result = collect_agent_registration(
        [agent()], zulip(subscriptions={(13, 5)}),
        FakePlane({"plane-13": {"id": "plane-13", "role": 20}}), now=NOW,
    )
    assert result.agents[0].zulip_channels == ["FreeForge"]


def test_a_desired_channel_that_does_not_exist_reads_as_not_subscribed():
    result = collect_agent_registration(
        [agent(desired_zulip_channels=["FreeForge", "no-such-channel"])],
        zulip(), FakePlane({}), now=NOW,
    )
    assert result.agents[0].zulip_channels == ["FreeForge"]


def test_unknown_zulip_user_and_plane_member_are_absent_not_errors():
    result = collect_agent_registration(
        [agent(zulip_user_id=99, plane_user_id="nobody")], zulip(), FakePlane({}), now=NOW
    )
    row = result.agents[0]
    assert row.zulip_present is False
    assert row.zulip_channels == []
    assert row.plane_present is False
    assert row.plane_role is None


def test_an_agent_without_ids_is_never_queried():
    fake = zulip()
    result = collect_agent_registration(
        [agent(zulip_user_id=None, plane_user_id="")], fake, FakePlane({}), now=NOW
    )
    assert result.agents[0].zulip_present is False
    assert fake.subscription_queries == []


def test_rows_are_sorted_by_slug_so_repeat_payloads_are_byte_stable():
    agents = [agent(slug="zeta", id="ag-2"), agent(slug="alpha", id="ag-3"), agent()]
    result = collect_agent_registration(agents, zulip(), FakePlane({}), now=NOW)
    assert [row.slug for row in result.agents] == ["agforge", "alpha", "zeta"]


def test_payload_carries_the_pinned_schema_and_an_iso_timestamp():
    payload = AgentRegistrationCollection(
        observed_at=NOW,
        agents=collect_agent_registration([agent()], zulip(), FakePlane({}), now=NOW).agents,
    ).as_payload()
    assert payload["schema_version"] == PAYLOAD_SCHEMA
    assert payload["observed_at"] == "2026-08-17T12:00:00+00:00"
    assert set(payload["agents"][0]) == {
        "slug", "zulip_present", "zulip_user_id", "zulip_is_active", "zulip_channels",
        "plane_present", "plane_user_id", "plane_role",
    }
