"""Operation-level tests for `nctl_core.workflow_episode` (Step 1: reads).

REST is mocked with `respx` against the real `NautobotClient`, mirroring `test_braindump.py`'s
convention for its REST-backed writes.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
import respx

from nctl_core.nautobot import NautobotClient
from nctl_core.workflow_episode import (
    list_episodes,
    show_episode,
    validate_workflow_episode_id,
)
from nctl_core.workflow_episode_errors import WorkflowEpisodeError

BASE_URL = "http://nautobot.test"
WE_ID = "11111111-1111-1111-1111-111111111111"
T0 = datetime(2026, 8, 4, tzinfo=timezone.utc)


def _client() -> NautobotClient:
    return NautobotClient(BASE_URL, "test-token")


def _row(*, id: str = WE_ID, title: str = "t", status: str = "candidate", raw_data: dict | None = None) -> dict:
    return {
        "id": id,
        "title": title,
        "status": status,
        "raw_data": raw_data or {},
        "created": T0.isoformat(),
        "last_updated": T0.isoformat(),
    }


def test_validate_workflow_episode_id_accepts_uuid():
    assert validate_workflow_episode_id(WE_ID) == WE_ID


def test_validate_workflow_episode_id_rejects_garbage():
    with pytest.raises(WorkflowEpisodeError) as exc_info:
        validate_workflow_episode_id("not-a-uuid")
    assert exc_info.value.code == "invalid_workflow_episode_id"


@respx.mock
def test_list_episodes_filters_by_status_client_side():
    respx.get(f"{BASE_URL}/api/plugins/intent-catalog/workflow-episodes/").mock(
        return_value=httpx.Response(
            200,
            json={"count": 3, "results": [
                _row(id=WE_ID, status="candidate"),
                _row(id="22222222-2222-2222-2222-222222222222", status="resolved"),
                _row(id="33333333-3333-3333-3333-333333333333", status="selected"),
            ]},
        )
    )
    items = list_episodes(_client(), statuses=frozenset({"candidate", "selected"}))
    assert {item.id for item in items} == {WE_ID, "33333333-3333-3333-3333-333333333333"}


@respx.mock
def test_list_episodes_statuses_none_returns_everything():
    respx.get(f"{BASE_URL}/api/plugins/intent-catalog/workflow-episodes/").mock(
        return_value=httpx.Response(200, json={"count": 2, "results": [_row(status="candidate"), _row(id="22222222-2222-2222-2222-222222222222", status="dismissed")]})
    )
    items = list_episodes(_client(), statuses=None)
    assert len(items) == 2


@respx.mock
def test_show_episode_returns_full_raw_data():
    raw_data = {"schema_version": 1, "report": {"summary": "s"}, "references": {"session_id": "x"}}
    respx.get(f"{BASE_URL}/api/plugins/intent-catalog/workflow-episodes/{WE_ID}/").mock(
        return_value=httpx.Response(200, json=_row(id=WE_ID, raw_data=raw_data))
    )
    record = show_episode(_client(), WE_ID)
    assert record.id == WE_ID
    assert record.raw_data == raw_data


@respx.mock
def test_show_episode_not_found():
    respx.get(f"{BASE_URL}/api/plugins/intent-catalog/workflow-episodes/{WE_ID}/").mock(
        return_value=httpx.Response(404, json={"detail": "not found"})
    )
    with pytest.raises(WorkflowEpisodeError) as exc_info:
        show_episode(_client(), WE_ID)
    assert exc_info.value.code == "workflow_episode_not_found"


def test_show_episode_rejects_invalid_id():
    with pytest.raises(WorkflowEpisodeError) as exc_info:
        show_episode(_client(), "not-a-uuid")
    assert exc_info.value.code == "invalid_workflow_episode_id"
