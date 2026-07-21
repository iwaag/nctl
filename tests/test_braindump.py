"""Operation-level tests for `nctl_core.braindump` create/update/list/show (Phase 2 Step 2.3).

Follows the `test_lifecycle_contract.py` pattern: GraphQL reads are monkeypatched at the
`fetch_braindump_show`/`fetch_braindump_list` call sites (isolating REST contract assertions from
GraphQL response shape, already covered by `test_sources_braindump.py`), while REST POST/PATCH are
mocked with `respx` against the real `NautobotClient`.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx

from nctl_core.braindump import (
    BraindumpConfirmationMismatchError,
    BraindumpNotFoundError,
    BraindumpValidationFailedError,
    BraindumpWriteRejectedError,
    InputConflictError,
    InputFileError,
    InputFileInvalidUtf8Error,
    InvalidAuthorshipError,
    InvalidBraindumpIdError,
    InvalidTextError,
    NoUpdateFieldsError,
    create_braindump,
    list_braindumps,
    resolve_text_input,
    show_braindump,
    update_braindump,
    validate_authorship,
    validate_braindump_id,
)
from nctl_core.nautobot import (
    NautobotAuthError,
    NautobotClient,
    NautobotConnectionError,
)
from nctl_core.sources.braindump import AlignmentReviewRead, BrainDumpRead

BASE_URL = "http://nautobot.test"
BD_ID = "11111111-1111-1111-1111-111111111111"
T0 = datetime(2026, 7, 20, tzinfo=timezone.utc)
T1 = datetime(2026, 7, 21, tzinfo=timezone.utc)


def _client() -> NautobotClient:
    return NautobotClient(BASE_URL, "test-token")


def _read(
    *,
    id: str = BD_ID,
    title: str = "title",
    body: str = "body",
    authorship: str = "user_direct",
    created: datetime = T0,
    last_updated: datetime = T0,
    review: AlignmentReviewRead | None = None,
) -> BrainDumpRead:
    return BrainDumpRead(
        id=id,
        title=title,
        body=body,
        authorship=authorship,  # type: ignore[arg-type]
        created=created,
        last_updated=last_updated,
        alignment_review=review,
    )


def _patch_show(monkeypatch, reads: list[BrainDumpRead | None]):
    calls = iter(reads)

    def fake_show(client, braindump_id):
        return next(calls)

    monkeypatch.setattr("nctl_core.braindump.fetch_braindump_show", fake_show)


def _patch_list(monkeypatch, reads: list[BrainDumpRead]):
    monkeypatch.setattr("nctl_core.braindump.fetch_braindump_list", lambda client: reads)


# -- resolve_text_input -----------------------------------------------------------------------


def test_resolve_text_input_literal():
    assert resolve_text_input(field_name="body", literal="hello", file=None) == "hello"


def test_resolve_text_input_file(tmp_path: Path):
    path = tmp_path / "body.txt"
    path.write_text("日本語\nmultiline\n", encoding="utf-8")
    assert resolve_text_input(field_name="body", literal=None, file=path) == "日本語\nmultiline\n"


def test_resolve_text_input_both_provided_conflicts(tmp_path: Path):
    path = tmp_path / "body.txt"
    path.write_text("x", encoding="utf-8")
    with pytest.raises(InputConflictError):
        resolve_text_input(field_name="body", literal="hello", file=path)


def test_resolve_text_input_neither_provided_conflicts():
    with pytest.raises(InputConflictError):
        resolve_text_input(field_name="body", literal=None, file=None)


def test_resolve_text_input_whitespace_only_rejected():
    with pytest.raises(InvalidTextError):
        resolve_text_input(field_name="body", literal="   \n  ", file=None)


def test_resolve_text_input_missing_file(tmp_path: Path):
    with pytest.raises(InputFileError):
        resolve_text_input(field_name="body", literal=None, file=tmp_path / "missing.txt")


def test_resolve_text_input_invalid_utf8(tmp_path: Path):
    path = tmp_path / "bad.txt"
    path.write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(InputFileInvalidUtf8Error):
        resolve_text_input(field_name="body", literal=None, file=path)


def test_resolve_text_input_preserves_exact_text_including_trailing_newline(tmp_path: Path):
    path = tmp_path / "body.txt"
    path.write_text("line one\nline two\n", encoding="utf-8")
    result = resolve_text_input(field_name="body", literal=None, file=path)
    assert result == "line one\nline two\n"
    assert result.endswith("\n")


# -- validate_authorship / validate_braindump_id ------------------------------------------------


def test_validate_authorship_accepts_both_values():
    assert validate_authorship("user_direct") == "user_direct"
    assert validate_authorship("agent_transcribed") == "agent_transcribed"


def test_validate_authorship_rejects_unknown_value():
    with pytest.raises(InvalidAuthorshipError):
        validate_authorship("admin")


def test_validate_braindump_id_canonicalizes():
    assert validate_braindump_id(BD_ID.upper()) == BD_ID


def test_validate_braindump_id_rejects_malformed():
    with pytest.raises(InvalidBraindumpIdError):
        validate_braindump_id("not-a-uuid")


# -- list / show --------------------------------------------------------------------------------


def test_list_braindumps_projects_compact_items(monkeypatch):
    review = AlignmentReviewRead(id="rev-1", summary="s", created=T1, last_updated=T1)
    _patch_list(monkeypatch, [_read(review=review)])

    with _client() as client:
        items = list_braindumps(client)

    assert len(items) == 1
    item = items[0]
    assert item.id == BD_ID
    assert item.review_present is True
    assert item.review_id == "rev-1"
    assert item.attention == "review_present"
    assert not hasattr(item, "body")


def test_show_braindump_not_found_raises(monkeypatch):
    _patch_show(monkeypatch, [None])

    with _client() as client:
        with pytest.raises(BraindumpNotFoundError):
            show_braindump(client, BD_ID)


def test_show_braindump_invalid_id_rejected_before_fetch(monkeypatch):
    def fail_show(client, braindump_id):
        raise AssertionError("must not fetch for a malformed id")

    monkeypatch.setattr("nctl_core.braindump.fetch_braindump_show", fail_show)

    with _client() as client:
        with pytest.raises(InvalidBraindumpIdError):
            show_braindump(client, "not-a-uuid")


def test_show_braindump_returns_full_record(monkeypatch):
    _patch_show(monkeypatch, [_read()])

    with _client() as client:
        record = show_braindump(client, BD_ID)

    assert record.title == "title"
    assert record.body == "body"
    assert record.alignment_review is None
    assert record.attention == "unreviewed"


# -- create --------------------------------------------------------------------------------------


@respx.mock
def test_create_sends_exact_fields_and_confirms(monkeypatch):
    _patch_show(monkeypatch, [_read(title="T", body="B", authorship="agent_transcribed")])
    post_route = respx.post(f"{BASE_URL}/api/plugins/intent-catalog/braindumps/").mock(
        return_value=httpx.Response(201, json={"id": BD_ID})
    )

    with _client() as client:
        record, changed = create_braindump(
            client, title="T", authorship="agent_transcribed", body="B"
        )

    assert post_route.call_count == 1
    assert json.loads(post_route.calls.last.request.content) == {
        "title": "T",
        "body": "B",
        "authorship": "agent_transcribed",
    }
    assert changed is True
    assert record.id == BD_ID


@respx.mock
def test_create_rejects_blank_title_before_any_request():
    respx.post(f"{BASE_URL}/api/plugins/intent-catalog/braindumps/").mock(
        return_value=httpx.Response(201, json={"id": BD_ID})
    )

    with _client() as client:
        with pytest.raises(InvalidTextError):
            create_braindump(client, title="   ", authorship="user_direct", body="B")


@respx.mock
def test_create_validation_failure_maps_to_validation_error():
    respx.post(f"{BASE_URL}/api/plugins/intent-catalog/braindumps/").mock(
        return_value=httpx.Response(400, json={"body": ["required"]})
    )

    with _client() as client:
        with pytest.raises(BraindumpValidationFailedError):
            create_braindump(client, title="T", authorship="user_direct", body="B")


@respx.mock
def test_create_server_error_maps_to_write_rejected():
    respx.post(f"{BASE_URL}/api/plugins/intent-catalog/braindumps/").mock(
        return_value=httpx.Response(500, text="boom")
    )

    with _client() as client:
        with pytest.raises(BraindumpWriteRejectedError):
            create_braindump(client, title="T", authorship="user_direct", body="B")


@respx.mock
def test_create_confirmation_mismatch_fails_closed(monkeypatch):
    _patch_show(monkeypatch, [_read(title="different")])
    respx.post(f"{BASE_URL}/api/plugins/intent-catalog/braindumps/").mock(
        return_value=httpx.Response(201, json={"id": BD_ID})
    )

    with _client() as client:
        with pytest.raises(BraindumpConfirmationMismatchError):
            create_braindump(client, title="T", authorship="user_direct", body="B")


@respx.mock
def test_create_auth_and_connection_failures_propagate():
    respx.post(f"{BASE_URL}/api/plugins/intent-catalog/braindumps/").mock(
        return_value=httpx.Response(403)
    )
    with _client() as client:
        with pytest.raises(NautobotAuthError):
            create_braindump(client, title="T", authorship="user_direct", body="B")


# -- update --------------------------------------------------------------------------------------


def test_update_requires_at_least_one_field(monkeypatch):
    def fail_show(client, braindump_id):
        raise AssertionError("must not fetch when no fields are supplied")

    monkeypatch.setattr("nctl_core.braindump.fetch_braindump_show", fail_show)

    with _client() as client:
        with pytest.raises(NoUpdateFieldsError):
            update_braindump(client, BD_ID)


@respx.mock
def test_update_sends_only_supplied_fields_and_confirms(monkeypatch):
    _patch_show(monkeypatch, [_read(title="old"), _read(title="new")])
    patch_route = respx.patch(f"{BASE_URL}/api/plugins/intent-catalog/braindumps/{BD_ID}/").mock(
        return_value=httpx.Response(200, json={})
    )

    with _client() as client:
        record, changed = update_braindump(client, BD_ID, title="new")

    assert json.loads(patch_route.calls.last.request.content) == {"title": "new"}
    assert changed is True
    assert record.title == "new"


@respx.mock
def test_update_no_op_when_already_matching(monkeypatch):
    _patch_show(monkeypatch, [_read(title="same")])
    patch_route = respx.patch(f"{BASE_URL}/api/plugins/intent-catalog/braindumps/{BD_ID}/").mock(
        return_value=httpx.Response(200, json={})
    )

    with _client() as client:
        record, changed = update_braindump(client, BD_ID, title="same")

    assert patch_route.call_count == 0
    assert changed is False
    assert record.title == "same"


def test_update_unknown_id_raises(monkeypatch):
    _patch_show(monkeypatch, [None])

    with _client() as client:
        with pytest.raises(BraindumpNotFoundError):
            update_braindump(client, BD_ID, title="new")


@respx.mock
def test_update_confirmation_mismatch_fails_closed(monkeypatch):
    _patch_show(monkeypatch, [_read(title="old"), _read(title="unexpected")])
    respx.patch(f"{BASE_URL}/api/plugins/intent-catalog/braindumps/{BD_ID}/").mock(
        return_value=httpx.Response(200, json={})
    )

    with _client() as client:
        with pytest.raises(BraindumpConfirmationMismatchError):
            update_braindump(client, BD_ID, title="new")


@respx.mock
def test_update_validation_failure_maps_to_validation_error(monkeypatch):
    _patch_show(monkeypatch, [_read(title="old")])
    respx.patch(f"{BASE_URL}/api/plugins/intent-catalog/braindumps/{BD_ID}/").mock(
        return_value=httpx.Response(400, json={"title": ["invalid"]})
    )

    with _client() as client:
        with pytest.raises(BraindumpValidationFailedError):
            update_braindump(client, BD_ID, title="new")


@respx.mock
def test_update_connection_failure_propagates(monkeypatch):
    _patch_show(monkeypatch, [_read(title="old")])
    respx.patch(f"{BASE_URL}/api/plugins/intent-catalog/braindumps/{BD_ID}/").mock(
        side_effect=httpx.ConnectError("refused")
    )

    with _client() as client:
        with pytest.raises(NautobotConnectionError):
            update_braindump(client, BD_ID, title="new")
