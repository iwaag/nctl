"""Operation-level tests for `nctl_core.braindump` (Phase 2 Steps 2.3-2.4: create/update/list/show,
review create-or-replace, and both delete operations).

Follows the `test_lifecycle_contract.py` pattern: GraphQL reads are monkeypatched at the
`fetch_braindump_show`/`fetch_braindump_list` call sites (isolating REST contract assertions from
GraphQL response shape, already covered by `test_sources_braindump.py`), while REST
POST/PATCH/DELETE are mocked with `respx` against the real `NautobotClient`.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx

from nctl_core.braindump import (
    complete_braindump,
    create_braindump,
    create_or_replace_review,
    delete_review,
    list_braindumps,
    purge_braindump,
    resolve_text_input,
    show_braindump,
    validate_authorship,
    validate_braindump_id,
    supersede_braindumps,
)
from nctl_core.braindump_errors import BraindumpError
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
        status: str = "active",
    completion_reason: str = "",
    created: datetime = T0,
    last_updated: datetime = T0,
    review: AlignmentReviewRead | None = None,
) -> BrainDumpRead:
    return BrainDumpRead(
        id=id,
        title=title,
        body=body,
        authorship=authorship,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        completion_reason=completion_reason,
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
    with pytest.raises(BraindumpError) as exc:
        resolve_text_input(field_name="body", literal="hello", file=path)


def test_resolve_text_input_neither_provided_conflicts():
    with pytest.raises(BraindumpError) as exc:
        resolve_text_input(field_name="body", literal=None, file=None)


def test_resolve_text_input_whitespace_only_rejected():
    with pytest.raises(BraindumpError) as exc:
        resolve_text_input(field_name="body", literal="   \n  ", file=None)


def test_resolve_text_input_missing_file(tmp_path: Path):
    with pytest.raises(BraindumpError) as exc:
        resolve_text_input(field_name="body", literal=None, file=tmp_path / "missing.txt")


def test_resolve_text_input_invalid_utf8(tmp_path: Path):
    path = tmp_path / "bad.txt"
    path.write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(BraindumpError) as exc:
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
    with pytest.raises(BraindumpError) as exc:
        validate_authorship("admin")


def test_validate_braindump_id_canonicalizes():
    assert validate_braindump_id(BD_ID.upper()) == BD_ID


def test_validate_braindump_id_rejects_malformed():
    with pytest.raises(BraindumpError) as exc:
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


def test_list_braindumps_excludes_completed_by_default(monkeypatch):
    _patch_list(monkeypatch, [_read(status="active"), _read(id="c", status="completed")])

    with _client() as client:
        items = list_braindumps(client)

    assert [item.id for item in items] == [BD_ID]


def test_list_braindumps_include_superseded_also_includes_completed(monkeypatch):
    _patch_list(monkeypatch, [_read(status="active"), _read(id="c", status="completed")])

    with _client() as client:
        items = list_braindumps(client, include_superseded=True)

    assert {item.id for item in items} == {BD_ID, "c"}


def test_show_braindump_not_found_raises(monkeypatch):
    _patch_show(monkeypatch, [None])

    with _client() as client:
        with pytest.raises(BraindumpError) as exc:
            show_braindump(client, BD_ID)


def test_show_braindump_invalid_id_rejected_before_fetch(monkeypatch):
    def fail_show(client, braindump_id):
        raise AssertionError("must not fetch for a malformed id")

    monkeypatch.setattr("nctl_core.braindump.fetch_braindump_show", fail_show)

    with _client() as client:
        with pytest.raises(BraindumpError) as exc:
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
        with pytest.raises(BraindumpError) as exc:
            create_braindump(client, title="   ", authorship="user_direct", body="B")


@respx.mock
def test_create_validation_failure_maps_to_validation_error():
    respx.post(f"{BASE_URL}/api/plugins/intent-catalog/braindumps/").mock(
        return_value=httpx.Response(400, json={"body": ["required"]})
    )

    with _client() as client:
        with pytest.raises(BraindumpError) as exc:
            create_braindump(client, title="T", authorship="user_direct", body="B")


@respx.mock
def test_create_server_error_maps_to_write_rejected():
    respx.post(f"{BASE_URL}/api/plugins/intent-catalog/braindumps/").mock(
        return_value=httpx.Response(500, text="boom")
    )

    with _client() as client:
        with pytest.raises(BraindumpError) as exc:
            create_braindump(client, title="T", authorship="user_direct", body="B")


@respx.mock
def test_create_confirmation_mismatch_fails_closed(monkeypatch):
    _patch_show(monkeypatch, [_read(title="different")])
    respx.post(f"{BASE_URL}/api/plugins/intent-catalog/braindumps/").mock(
        return_value=httpx.Response(201, json={"id": BD_ID})
    )

    with _client() as client:
        with pytest.raises(BraindumpError) as exc:
            create_braindump(client, title="T", authorship="user_direct", body="B")


@respx.mock
def test_create_auth_and_connection_failures_propagate():
    respx.post(f"{BASE_URL}/api/plugins/intent-catalog/braindumps/").mock(
        return_value=httpx.Response(403)
    )
    with _client() as client:
        with pytest.raises(NautobotAuthError):
            create_braindump(client, title="T", authorship="user_direct", body="B")


# -- supersede -------------------------------------------------------------------------------


@respx.mock
def test_supersede_posts_exact_ids_and_confirms_all_statuses(monkeypatch):
    old_id = BD_ID
    second_id = "33333333-3333-3333-3333-333333333333"
    new_id = "44444444-4444-4444-4444-444444444444"
    _patch_show(
        monkeypatch,
        [
            _read(id=new_id, title="New", body="Replacement", authorship="agent_transcribed", status="active"),
            _read(id=old_id, status="superseded"),
            _read(id=second_id, status="superseded"),
        ],
    )
    route = respx.post(f"{BASE_URL}/api/plugins/intent-catalog/braindumps/supersede/").mock(
        return_value=httpx.Response(201, json={"braindump": {"id": new_id}, "superseded_ids": [old_id, second_id]})
    )
    with _client() as client:
        record, superseded_ids, changed = supersede_braindumps(
            client, old_ids=[old_id, second_id], title="New", authorship="agent_transcribed", body="Replacement"
        )
    assert json.loads(route.calls.last.request.content) == {
        "old_ids": [old_id, second_id], "title": "New", "body": "Replacement", "authorship": "agent_transcribed"
    }
    assert record.id == new_id
    assert superseded_ids == [old_id, second_id]
    assert changed is True


@respx.mock
def test_supersede_server_rejection_is_reported_without_confirmation_fetch(monkeypatch):
    def fail_show(client, braindump_id):
        raise AssertionError("rejected replacement must not be confirmed")
    monkeypatch.setattr("nctl_core.braindump.fetch_braindump_show", fail_show)
    respx.post(f"{BASE_URL}/api/plugins/intent-catalog/braindumps/supersede/").mock(
        return_value=httpx.Response(400, json={"old_ids": ["not active"]})
    )
    with _client() as client:
        with pytest.raises(BraindumpError) as exc:
            supersede_braindumps(client, old_ids=[BD_ID], title="New", authorship="user_direct", body="Replacement")
    assert exc.value.code == "braindump_supersede_invalid"


# -- complete ---------------------------------------------------------------------------------------


@respx.mock
def test_complete_posts_reason_and_confirms(monkeypatch):
    _patch_show(monkeypatch, [_read(status="completed", completion_reason="Node retired.")])
    route = respx.post(f"{BASE_URL}/api/plugins/intent-catalog/braindumps/{BD_ID}/complete/").mock(
        return_value=httpx.Response(200, json={"id": BD_ID, "status": "completed"})
    )

    with _client() as client:
        record, changed = complete_braindump(client, BD_ID, reason="Node retired.")

    assert json.loads(route.calls.last.request.content) == {"reason": "Node retired."}
    assert record.status == "completed"
    assert record.completion_reason == "Node retired."
    assert changed is True


@respx.mock
def test_complete_rejects_blank_reason_before_any_request(monkeypatch):
    def fail_show(client, braindump_id):
        raise AssertionError("must not fetch for blank reason")
    monkeypatch.setattr("nctl_core.braindump.fetch_braindump_show", fail_show)

    with _client() as client:
        with pytest.raises(BraindumpError) as exc:
            complete_braindump(client, BD_ID, reason="   ")
    assert exc.value.code == "invalid_text"


@respx.mock
def test_complete_non_active_row_maps_to_ineligible():
    respx.post(f"{BASE_URL}/api/plugins/intent-catalog/braindumps/{BD_ID}/complete/").mock(
        return_value=httpx.Response(409, json={"detail": "Braindump is not active."})
    )

    with _client() as client:
        with pytest.raises(BraindumpError) as exc:
            complete_braindump(client, BD_ID, reason="done")
    assert exc.value.code == "braindump_complete_ineligible"


@respx.mock
def test_complete_confirmation_mismatch_fails_closed(monkeypatch):
    _patch_show(monkeypatch, [_read(status="active")])
    respx.post(f"{BASE_URL}/api/plugins/intent-catalog/braindumps/{BD_ID}/complete/").mock(
        return_value=httpx.Response(200, json={"id": BD_ID, "status": "completed"})
    )

    with _client() as client:
        with pytest.raises(BraindumpError) as exc:
            complete_braindump(client, BD_ID, reason="done")
    assert exc.value.code == "braindump_complete_confirmation_mismatch"


# -- review create-or-replace ---------------------------------------------------------------------

REVIEW_ID = "22222222-2222-2222-2222-222222222222"


def _review(id: str = REVIEW_ID, summary: str = "s", last_updated: datetime = T0):
    return AlignmentReviewRead(id=id, summary=summary, created=last_updated, last_updated=last_updated)


@respx.mock
def test_review_creates_when_absent(monkeypatch):
    _patch_show(
        monkeypatch,
        [_read(review=None), _read(review=_review(summary="new summary"))],
    )
    post_route = respx.post(f"{BASE_URL}/api/plugins/intent-catalog/alignment-reviews/").mock(
        return_value=httpx.Response(201, json={"id": REVIEW_ID})
    )

    with _client() as client:
        record, action = create_or_replace_review(client, BD_ID, summary="new summary")

    assert post_route.call_count == 1
    assert json.loads(post_route.calls.last.request.content) == {
        "braindump": BD_ID,
        "summary": "new summary",
    }
    assert action == "created"
    assert record.alignment_review.summary == "new summary"


@respx.mock
def test_review_replaces_when_present(monkeypatch):
    _patch_show(
        monkeypatch,
        [_read(review=_review(summary="old")), _read(review=_review(summary="new"))],
    )
    patch_route = respx.patch(
        f"{BASE_URL}/api/plugins/intent-catalog/alignment-reviews/{REVIEW_ID}/"
    ).mock(return_value=httpx.Response(200, json={}))

    with _client() as client:
        record, action = create_or_replace_review(client, BD_ID, summary="new")

    assert patch_route.call_count == 1
    assert json.loads(patch_route.calls.last.request.content) == {"summary": "new"}
    assert action == "replaced"
    assert record.alignment_review.summary == "new"


@respx.mock
def test_review_replace_refreshes_timestamp_even_with_identical_summary(monkeypatch):
    _patch_show(
        monkeypatch,
        [
            _read(review=_review(summary="same", last_updated=T0)),
            _read(review=_review(summary="same", last_updated=T1)),
        ],
    )
    patch_route = respx.patch(
        f"{BASE_URL}/api/plugins/intent-catalog/alignment-reviews/{REVIEW_ID}/"
    ).mock(return_value=httpx.Response(200, json={}))

    with _client() as client:
        record, action = create_or_replace_review(client, BD_ID, summary="same")

    assert patch_route.call_count == 1
    assert action == "replaced"
    assert record.alignment_review.last_updated == T1


@respx.mock
def test_review_rejects_blank_summary_before_any_request(monkeypatch):
    def fail_show(client, braindump_id):
        raise AssertionError("must not fetch for blank summary")

    monkeypatch.setattr("nctl_core.braindump.fetch_braindump_show", fail_show)

    with _client() as client:
        with pytest.raises(BraindumpError) as exc:
            create_or_replace_review(client, BD_ID, summary="   ")


@respx.mock
def test_review_unknown_braindump_raises(monkeypatch):
    _patch_show(monkeypatch, [None])

    with _client() as client:
        with pytest.raises(BraindumpError) as exc:
            create_or_replace_review(client, BD_ID, summary="s")


@respx.mock
def test_review_race_recovery_patches_now_current_review(monkeypatch):
    # First show: no review. POST 400s (another writer won the race). Refetch shows a review
    # that appeared in the meantime; recovery PATCHes it. Final refetch confirms the new summary.
    _patch_show(
        monkeypatch,
        [
            _read(review=None),
            _read(review=_review(summary="racer's summary")),
            _read(review=_review(summary="mine")),
        ],
    )
    respx.post(f"{BASE_URL}/api/plugins/intent-catalog/alignment-reviews/").mock(
        return_value=httpx.Response(400, json={"braindump": ["already exists"]})
    )
    patch_route = respx.patch(
        f"{BASE_URL}/api/plugins/intent-catalog/alignment-reviews/{REVIEW_ID}/"
    ).mock(return_value=httpx.Response(200, json={}))

    with _client() as client:
        record, action = create_or_replace_review(client, BD_ID, summary="mine")

    assert patch_route.call_count == 1
    assert action == "replaced"
    assert record.alignment_review.summary == "mine"


@respx.mock
def test_review_genuine_validation_failure_is_not_treated_as_race(monkeypatch):
    # No review before or after the failed POST -> a real validation failure, not a race.
    _patch_show(monkeypatch, [_read(review=None), _read(review=None)])
    respx.post(f"{BASE_URL}/api/plugins/intent-catalog/alignment-reviews/").mock(
        return_value=httpx.Response(400, json={"summary": ["required"]})
    )

    with _client() as client:
        with pytest.raises(BraindumpError) as exc:
            create_or_replace_review(client, BD_ID, summary="s")


@respx.mock
def test_review_race_recovery_patch_failure_propagates(monkeypatch):
    _patch_show(
        monkeypatch,
        [_read(review=None), _read(review=_review(summary="racer's summary"))],
    )
    respx.post(f"{BASE_URL}/api/plugins/intent-catalog/alignment-reviews/").mock(
        return_value=httpx.Response(400, json={"braindump": ["already exists"]})
    )
    respx.patch(f"{BASE_URL}/api/plugins/intent-catalog/alignment-reviews/{REVIEW_ID}/").mock(
        return_value=httpx.Response(500, text="boom")
    )

    with _client() as client:
        with pytest.raises(BraindumpError) as exc:
            create_or_replace_review(client, BD_ID, summary="mine")


@respx.mock
def test_review_confirmation_mismatch_fails_closed(monkeypatch):
    _patch_show(monkeypatch, [_read(review=None), _read(review=_review(summary="wrong"))])
    respx.post(f"{BASE_URL}/api/plugins/intent-catalog/alignment-reviews/").mock(
        return_value=httpx.Response(201, json={"id": REVIEW_ID})
    )

    with _client() as client:
        with pytest.raises(BraindumpError) as exc:
            create_or_replace_review(client, BD_ID, summary="mine")


@respx.mock
def test_review_authorization_and_connection_failures_propagate(monkeypatch):
    _patch_show(monkeypatch, [_read(review=None)])
    respx.post(f"{BASE_URL}/api/plugins/intent-catalog/alignment-reviews/").mock(
        side_effect=httpx.ConnectError("refused")
    )

    with _client() as client:
        with pytest.raises(NautobotConnectionError):
            create_or_replace_review(client, BD_ID, summary="mine")


# -- deletes ---------------------------------------------------------------------------------------


@respx.mock
def test_purge_plan_and_apply_use_only_the_exact_endpoint():
    url = f"{BASE_URL}/api/plugins/intent-catalog/braindumps/{BD_ID}/purge/"
    plan = respx.post(url).mock(
        return_value=httpx.Response(200, json={"outcome": "planned", "braindump": _read(status="superseded").model_dump(mode="json"), "alignment_review_present": False})
    )
    apply = respx.delete(url).mock(
        return_value=httpx.Response(200, json={"outcome": "purged", "braindump": _read(status="superseded").model_dump(mode="json"), "alignment_review_present": False})
    )

    with _client() as client:
        planned = purge_braindump(client, BD_ID, apply=False)
        purged = purge_braindump(client, BD_ID, apply=True)

    assert plan.call_count == 1
    assert apply.call_count == 1
    assert planned.outcome == "planned"
    assert purged.outcome == "purged"
    assert purged.braindump.status == "superseded"


@respx.mock
def test_purge_is_idempotent_when_server_reports_already_purged():
    respx.delete(f"{BASE_URL}/api/plugins/intent-catalog/braindumps/{BD_ID}/purge/").mock(
        return_value=httpx.Response(200, json={"outcome": "already_purged", "braindump_id": BD_ID})
    )

    with _client() as client:
        result = purge_braindump(client, BD_ID, apply=True)

    assert result.outcome == "already_purged"
    assert result.braindump is None


@respx.mock
def test_purge_active_row_is_rejected():
    respx.delete(f"{BASE_URL}/api/plugins/intent-catalog/braindumps/{BD_ID}/purge/").mock(
        return_value=httpx.Response(409, json={"outcome": "ineligible"})
    )

    with _client() as client:
        with pytest.raises(BraindumpError) as exc:
            purge_braindump(client, BD_ID, apply=True)

    assert exc.value.code == "braindump_purge_ineligible"


@respx.mock
def test_delete_review_only_preserves_braindump(monkeypatch):
    _patch_show(monkeypatch, [_read(review=_review()), _read(review=None)])
    delete_route = respx.delete(
        f"{BASE_URL}/api/plugins/intent-catalog/alignment-reviews/{REVIEW_ID}/"
    ).mock(return_value=httpx.Response(204))

    with _client() as client:
        deleted, review_id = delete_review(client, BD_ID)

    assert delete_route.call_count == 1
    assert deleted is True
    assert review_id == REVIEW_ID


@respx.mock
def test_delete_review_missing_review_is_idempotent_no_op(monkeypatch):
    _patch_show(monkeypatch, [_read(review=None)])
    delete_route = respx.delete(url__regex=r".*/alignment-reviews/.*").mock(
        return_value=httpx.Response(204)
    )

    with _client() as client:
        deleted, review_id = delete_review(client, BD_ID)

    assert delete_route.call_count == 0
    assert deleted is False
    assert review_id is None


def test_delete_review_unknown_braindump_raises(monkeypatch):
    _patch_show(monkeypatch, [None])

    with _client() as client:
        with pytest.raises(BraindumpError) as exc:
            delete_review(client, BD_ID)


@respx.mock
def test_delete_review_rejected_raises(monkeypatch):
    _patch_show(monkeypatch, [_read(review=_review())])
    respx.delete(f"{BASE_URL}/api/plugins/intent-catalog/alignment-reviews/{REVIEW_ID}/").mock(
        return_value=httpx.Response(500, text="boom")
    )

    with _client() as client:
        with pytest.raises(BraindumpError) as exc:
            delete_review(client, BD_ID)


@respx.mock
def test_delete_review_confirmation_mismatch_fails_closed(monkeypatch):
    _patch_show(monkeypatch, [_read(review=_review()), _read(review=_review())])
    respx.delete(f"{BASE_URL}/api/plugins/intent-catalog/alignment-reviews/{REVIEW_ID}/").mock(
        return_value=httpx.Response(204)
    )

    with _client() as client:
        with pytest.raises(BraindumpError) as exc:
            delete_review(client, BD_ID)
