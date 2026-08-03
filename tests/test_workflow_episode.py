"""Operation-level tests for `nctl_core.workflow_episode` (Step 1: reads).

REST is mocked with `respx` against the real `NautobotClient`, mirroring `test_braindump.py`'s
convention for its REST-backed writes.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx

from nctl_core.nautobot import NautobotClient
from nctl_core.workflow_episode import (
    create_episode,
    list_episodes,
    resolve_json_object_input,
    resolve_optional_json_object_input,
    show_episode,
    validate_workflow_episode_id,
    write_namespace,
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


# -- input resolution --------------------------------------------------------------------------


def test_resolve_json_object_input_from_literal():
    assert resolve_json_object_input(field_name="data", literal='{"a": 1}', file=None) == {"a": 1}


def test_resolve_json_object_input_from_file(tmp_path: Path):
    path = tmp_path / "doc.json"
    path.write_text('{"a": 1}', encoding="utf-8")
    assert resolve_json_object_input(field_name="data", literal=None, file=path) == {"a": 1}


def test_resolve_json_object_input_conflict_both():
    with pytest.raises(WorkflowEpisodeError) as exc_info:
        resolve_json_object_input(field_name="data", literal="{}", file=Path("x"))
    assert exc_info.value.code == "input_conflict"


def test_resolve_json_object_input_conflict_neither():
    with pytest.raises(WorkflowEpisodeError) as exc_info:
        resolve_json_object_input(field_name="data", literal=None, file=None)
    assert exc_info.value.code == "input_conflict"


def test_resolve_json_object_input_rejects_bad_json():
    with pytest.raises(WorkflowEpisodeError) as exc_info:
        resolve_json_object_input(field_name="data", literal="not json", file=None)
    assert exc_info.value.code == "invalid_json"


def test_resolve_json_object_input_rejects_non_dict_json():
    with pytest.raises(WorkflowEpisodeError) as exc_info:
        resolve_json_object_input(field_name="data", literal="[1, 2]", file=None)
    assert exc_info.value.code == "invalid_namespace_payload"


def test_resolve_optional_json_object_input_neither_returns_none():
    assert resolve_optional_json_object_input(field_name="raw_data", literal=None, file=None) is None


def test_resolve_optional_json_object_input_delegates_when_given():
    assert resolve_optional_json_object_input(field_name="raw_data", literal='{"a": 1}', file=None) == {"a": 1}


# -- create -------------------------------------------------------------------------------------


@respx.mock
def test_create_episode_sends_exact_fields():
    route = respx.post(f"{BASE_URL}/api/plugins/intent-catalog/workflow-episodes/").mock(
        return_value=httpx.Response(201, json=_row(title="T", raw_data={"report": {"summary": "s"}}))
    )
    record, changed = create_episode(_client(), title="T", raw_data={"report": {"summary": "s"}})
    assert json.loads(route.calls.last.request.content) == {"title": "T", "raw_data": {"report": {"summary": "s"}}}
    assert changed is True
    assert record.title == "T"


@respx.mock
def test_create_episode_omits_raw_data_when_none():
    route = respx.post(f"{BASE_URL}/api/plugins/intent-catalog/workflow-episodes/").mock(
        return_value=httpx.Response(201, json=_row(title="T"))
    )
    create_episode(_client(), title="T", raw_data=None)
    assert json.loads(route.calls.last.request.content) == {"title": "T"}


def test_create_episode_rejects_blank_title_before_any_request():
    with pytest.raises(WorkflowEpisodeError) as exc_info:
        create_episode(_client(), title="   ", raw_data=None)
    assert exc_info.value.code == "invalid_text"


@respx.mock
def test_create_episode_maps_400_to_validation_failed():
    respx.post(f"{BASE_URL}/api/plugins/intent-catalog/workflow-episodes/").mock(
        return_value=httpx.Response(400, json={"raw_data": ["unknown_namespace: ..."]})
    )
    with pytest.raises(WorkflowEpisodeError) as exc_info:
        create_episode(_client(), title="T", raw_data={"typo": {}})
    assert exc_info.value.code == "workflow_episode_validation_failed"


# -- namespace writes -----------------------------------------------------------------------


@respx.mock
def test_write_namespace_sends_exact_payload():
    route = respx.post(f"{BASE_URL}/api/plugins/intent-catalog/workflow-episodes/{WE_ID}/assessment/").mock(
        return_value=httpx.Response(200, json=_row(id=WE_ID, raw_data={"assessment": {"verdict": "promote"}}))
    )
    record = write_namespace(_client(), WE_ID, "assessment", {"verdict": "promote"})
    assert json.loads(route.calls.last.request.content) == {"verdict": "promote"}
    assert record.raw_data == {"assessment": {"verdict": "promote"}}


@respx.mock
def test_write_namespace_maps_400_to_validation_failed():
    respx.post(f"{BASE_URL}/api/plugins/intent-catalog/workflow-episodes/{WE_ID}/assessment/").mock(
        return_value=httpx.Response(400, json={"assessment": ["This field must be a JSON object."]})
    )
    with pytest.raises(WorkflowEpisodeError) as exc_info:
        write_namespace(_client(), WE_ID, "assessment", {"verdict": "x"})
    assert exc_info.value.code == "workflow_episode_validation_failed"


@respx.mock
def test_write_namespace_not_found():
    respx.post(f"{BASE_URL}/api/plugins/intent-catalog/workflow-episodes/{WE_ID}/assessment/").mock(
        return_value=httpx.Response(404, json={"detail": "not found"})
    )
    with pytest.raises(WorkflowEpisodeError) as exc_info:
        write_namespace(_client(), WE_ID, "assessment", {"verdict": "x"})
    assert exc_info.value.code == "workflow_episode_not_found"
