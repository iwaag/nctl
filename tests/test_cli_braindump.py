"""CLI-only tests for `nctl braindump *` (Phase 2 Step 2.5).

Mocks the `nctl_core.braindump.build_braindump_*` core boundary throughout, per plan.md's "Mock the
core boundary in CLI-only tests so business behavior remains covered in Steps 2.2-2.4" instruction.
Confirmation/`--yes`/`--json` gating (plan.md Decision 7) is exercised directly since it lives in
the CLI layer (`_confirm_destructive`), not in the mocked core builders.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from typer.testing import CliRunner

import nctl_core.cli.main as main
from nctl_core.braindump import (
    AlignmentReviewRecord,
    BraindumpCreateData,
    BraindumpDeleteData,
    BraindumpListData,
    BraindumpReviewData,
    BraindumpReviewDeleteData,
    BraindumpShowData,
    BraindumpUpdateData,
    BrainDumpListItem,
    BrainDumpRecord,
)
from nctl_core.output import Envelope, EnvelopeError

runner = CliRunner()
BD_ID = "11111111-1111-1111-1111-111111111111"
REVIEW_ID = "22222222-2222-2222-2222-222222222222"
T0 = datetime(2026, 7, 20, tzinfo=timezone.utc)


def _record(**overrides) -> BrainDumpRecord:
    fields = dict(
        id=BD_ID,
        title="my title",
        body="my body",
        authorship="user_direct",
        created=T0,
        last_updated=T0,
        review_present=False,
        attention="unreviewed",
        alignment_review=None,
    )
    fields.update(overrides)
    return BrainDumpRecord(**fields)


def _review_record(summary: str = "summary") -> AlignmentReviewRecord:
    return AlignmentReviewRecord(id=REVIEW_ID, summary=summary, created=T0, last_updated=T0)


def _error_envelope(schema: str, data, code: str) -> Envelope:
    return Envelope.build(schema, data, [EnvelopeError(code=code, message="boom")])


def _setup(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: object())


# -- list ------------------------------------------------------------------------------------


def test_list_prints_text(monkeypatch):
    _setup(monkeypatch)
    item = BrainDumpListItem(
        id=BD_ID, title="t", authorship="user_direct", created=T0, last_updated=T0,
        review_present=False, review_id=None, review_last_updated=None, attention="unreviewed",
    )
    monkeypatch.setattr(
        main, "build_braindump_list",
        lambda cfg: Envelope.build("nctl.braindump.list.v1", BraindumpListData(items=[item], count=1)),
    )

    result = runner.invoke(main.app, ["braindump", "list"])

    assert result.exit_code == 0
    assert BD_ID in result.stdout
    assert "Unreviewed" in result.stdout


def test_list_json(monkeypatch):
    _setup(monkeypatch)
    monkeypatch.setattr(
        main, "build_braindump_list",
        lambda cfg: Envelope.build("nctl.braindump.list.v1", BraindumpListData(items=[], count=0)),
    )

    result = runner.invoke(main.app, ["braindump", "list", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema"] == "nctl.braindump.list.v1"
    assert payload["data"]["count"] == 0


# -- show --------------------------------------------------------------------------------------


def test_show_found(monkeypatch):
    _setup(monkeypatch)
    captured = {}

    def fake_show(cfg, braindump_id):
        captured["id"] = braindump_id
        return Envelope.build("nctl.braindump.show.v1", BraindumpShowData(braindump=_record()))

    monkeypatch.setattr(main, "build_braindump_show", fake_show)

    result = runner.invoke(main.app, ["braindump", "show", BD_ID])

    assert result.exit_code == 0
    assert captured["id"] == BD_ID
    assert "User-originated Braindump" in result.stdout
    assert "AI Alignment Review" in result.stdout
    assert "Unreviewed" in result.stdout


def test_show_with_review_labels_separately(monkeypatch):
    _setup(monkeypatch)
    record = _record(review_present=True, attention="review_present", alignment_review=_review_record())
    monkeypatch.setattr(
        main, "build_braindump_show",
        lambda cfg, braindump_id: Envelope.build("nctl.braindump.show.v1", BraindumpShowData(braindump=record)),
    )

    result = runner.invoke(main.app, ["braindump", "show", BD_ID])

    assert result.exit_code == 0
    assert "summary" in result.stdout


def test_show_not_found_is_usage_exit(monkeypatch):
    _setup(monkeypatch)
    monkeypatch.setattr(
        main, "build_braindump_show",
        lambda cfg, braindump_id: _error_envelope("nctl.braindump.show.v1", BraindumpShowData(), "braindump_not_found"),
    )

    result = runner.invoke(main.app, ["braindump", "show", BD_ID])

    assert result.exit_code == 2


def test_show_invalid_id_is_usage_exit(monkeypatch):
    _setup(monkeypatch)
    monkeypatch.setattr(
        main, "build_braindump_show",
        lambda cfg, braindump_id: _error_envelope("nctl.braindump.show.v1", BraindumpShowData(), "invalid_braindump_id"),
    )

    result = runner.invoke(main.app, ["braindump", "show", "not-a-uuid"])

    assert result.exit_code == 2


def test_show_connection_failure_is_failure_exit(monkeypatch):
    _setup(monkeypatch)
    monkeypatch.setattr(
        main, "build_braindump_show",
        lambda cfg, braindump_id: _error_envelope(
            "nctl.braindump.show.v1", BraindumpShowData(), "nautobot_connection_error"
        ),
    )

    result = runner.invoke(main.app, ["braindump", "show", BD_ID])

    assert result.exit_code == 1


# -- create ------------------------------------------------------------------------------------


def test_create_literal_body_passes_through(monkeypatch):
    _setup(monkeypatch)
    captured = {}

    def fake_create(cfg, *, title, authorship, body=None, body_file=None):
        captured.update(title=title, authorship=authorship, body=body, body_file=body_file)
        return Envelope.build(
            "nctl.braindump.create.v1", BraindumpCreateData(braindump=_record(title=title), changed=True)
        )

    monkeypatch.setattr(main, "build_braindump_create", fake_create)

    result = runner.invoke(
        main.app,
        ["braindump", "create", "--title", "T", "--authorship", "user_direct", "--body", "hello"],
    )

    assert result.exit_code == 0
    assert captured == {"title": "T", "authorship": "user_direct", "body": "hello", "body_file": None}
    assert "created braindump" in result.stdout


def test_create_file_mode_passes_path(monkeypatch, tmp_path):
    _setup(monkeypatch)
    path = tmp_path / "body.txt"
    path.write_text("file body", encoding="utf-8")
    captured = {}

    def fake_create(cfg, *, title, authorship, body=None, body_file=None):
        captured.update(body=body, body_file=body_file)
        return Envelope.build(
            "nctl.braindump.create.v1", BraindumpCreateData(braindump=_record(), changed=True)
        )

    monkeypatch.setattr(main, "build_braindump_create", fake_create)

    result = runner.invoke(
        main.app,
        ["braindump", "create", "--title", "T", "--authorship", "agent_transcribed", "--file", str(path)],
    )

    assert result.exit_code == 0
    assert captured["body"] is None
    assert str(captured["body_file"]) == str(path)


def test_create_json_output(monkeypatch):
    _setup(monkeypatch)
    monkeypatch.setattr(
        main, "build_braindump_create",
        lambda cfg, **kw: Envelope.build(
            "nctl.braindump.create.v1", BraindumpCreateData(braindump=_record(), changed=True)
        ),
    )

    result = runner.invoke(
        main.app,
        ["braindump", "create", "--title", "T", "--authorship", "user_direct", "--body", "b", "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema"] == "nctl.braindump.create.v1"
    assert payload["data"]["changed"] is True


def test_create_invalid_authorship_choice_is_usage_exit(monkeypatch):
    _setup(monkeypatch)

    result = runner.invoke(
        main.app,
        ["braindump", "create", "--title", "T", "--authorship", "admin", "--body", "b"],
    )

    assert result.exit_code != 0  # click argument-parsing usage error


def test_create_mutual_exclusion_conflict_is_usage_exit(monkeypatch, tmp_path):
    _setup(monkeypatch)
    path = tmp_path / "b.txt"
    path.write_text("x", encoding="utf-8")
    monkeypatch.setattr(
        main, "build_braindump_create",
        lambda cfg, **kw: _error_envelope("nctl.braindump.create.v1", BraindumpCreateData(), "input_conflict"),
    )

    result = runner.invoke(
        main.app,
        ["braindump", "create", "--title", "T", "--authorship", "user_direct", "--body", "b", "--file", str(path)],
    )

    assert result.exit_code == 2


def test_create_whitespace_only_body_is_usage_exit(monkeypatch):
    _setup(monkeypatch)
    monkeypatch.setattr(
        main, "build_braindump_create",
        lambda cfg, **kw: _error_envelope("nctl.braindump.create.v1", BraindumpCreateData(), "invalid_text"),
    )

    result = runner.invoke(
        main.app,
        ["braindump", "create", "--title", "T", "--authorship", "user_direct", "--body", "   "],
    )

    assert result.exit_code == 2


def test_create_missing_file_is_usage_exit(monkeypatch):
    _setup(monkeypatch)
    monkeypatch.setattr(
        main, "build_braindump_create",
        lambda cfg, **kw: _error_envelope("nctl.braindump.create.v1", BraindumpCreateData(), "input_file_error"),
    )

    result = runner.invoke(
        main.app,
        ["braindump", "create", "--title", "T", "--authorship", "user_direct", "--file", "/no/such/file"],
    )

    assert result.exit_code == 2


def test_create_invalid_utf8_file_is_usage_exit(monkeypatch):
    _setup(monkeypatch)
    monkeypatch.setattr(
        main, "build_braindump_create",
        lambda cfg, **kw: _error_envelope(
            "nctl.braindump.create.v1", BraindumpCreateData(), "input_file_invalid_utf8"
        ),
    )

    result = runner.invoke(
        main.app,
        ["braindump", "create", "--title", "T", "--authorship", "user_direct", "--file", "/some/file"],
    )

    assert result.exit_code == 2


def test_create_validation_failure_is_failure_exit(monkeypatch):
    _setup(monkeypatch)
    monkeypatch.setattr(
        main, "build_braindump_create",
        lambda cfg, **kw: _error_envelope(
            "nctl.braindump.create.v1", BraindumpCreateData(), "braindump_validation_failed"
        ),
    )

    result = runner.invoke(
        main.app,
        ["braindump", "create", "--title", "T", "--authorship", "user_direct", "--body", "b"],
    )

    assert result.exit_code == 1


# -- update ------------------------------------------------------------------------------------


def test_update_passes_only_supplied_fields(monkeypatch):
    _setup(monkeypatch)
    captured = {}

    def fake_update(cfg, braindump_id, *, title=None, authorship=None, body=None, body_file=None):
        captured.update(
            braindump_id=braindump_id, title=title, authorship=authorship, body=body, body_file=body_file
        )
        return Envelope.build(
            "nctl.braindump.update.v1", BraindumpUpdateData(braindump=_record(), changed=True)
        )

    monkeypatch.setattr(main, "build_braindump_update", fake_update)

    result = runner.invoke(main.app, ["braindump", "update", BD_ID, "--title", "new"])

    assert result.exit_code == 0
    assert captured == {
        "braindump_id": BD_ID, "title": "new", "authorship": None, "body": None, "body_file": None,
    }


def test_update_no_fields_supplied_is_usage_exit(monkeypatch):
    _setup(monkeypatch)
    monkeypatch.setattr(
        main, "build_braindump_update",
        lambda cfg, braindump_id, **kw: _error_envelope(
            "nctl.braindump.update.v1", BraindumpUpdateData(), "no_update_fields"
        ),
    )

    result = runner.invoke(main.app, ["braindump", "update", BD_ID])

    assert result.exit_code == 2


def test_update_no_change_text(monkeypatch):
    _setup(monkeypatch)
    monkeypatch.setattr(
        main, "build_braindump_update",
        lambda cfg, braindump_id, **kw: Envelope.build(
            "nctl.braindump.update.v1", BraindumpUpdateData(braindump=_record(), changed=False)
        ),
    )

    result = runner.invoke(main.app, ["braindump", "update", BD_ID, "--title", "my title"])

    assert result.exit_code == 0
    assert "no change" in result.stdout


# -- delete ------------------------------------------------------------------------------------


def test_delete_with_yes_skips_prompt(monkeypatch):
    _setup(monkeypatch)
    called = {}

    def fake_delete(cfg, braindump_id):
        called["id"] = braindump_id
        return Envelope.build(
            "nctl.braindump.delete.v1",
            BraindumpDeleteData(id=braindump_id, title="t", deleted=True, review_deleted=True),
        )

    monkeypatch.setattr(main, "build_braindump_delete", fake_delete)

    result = runner.invoke(main.app, ["braindump", "delete", BD_ID, "--yes"])

    assert result.exit_code == 0
    assert called["id"] == BD_ID
    assert "review also deleted" in result.stdout


def test_delete_declined_prompt_performs_no_write(monkeypatch):
    _setup(monkeypatch)
    monkeypatch.setattr(
        main, "build_braindump_delete", lambda cfg, braindump_id: (_ for _ in ()).throw(
            AssertionError("must not delete without confirmation")
        )
    )

    result = runner.invoke(main.app, ["braindump", "delete", BD_ID], input="n\n")

    assert result.exit_code == 2


def test_delete_confirmed_prompt_proceeds(monkeypatch):
    _setup(monkeypatch)
    monkeypatch.setattr(
        main, "build_braindump_delete",
        lambda cfg, braindump_id: Envelope.build(
            "nctl.braindump.delete.v1",
            BraindumpDeleteData(id=braindump_id, title="t", deleted=True, review_deleted=False),
        ),
    )

    result = runner.invoke(main.app, ["braindump", "delete", BD_ID], input="y\n")

    assert result.exit_code == 0


def test_delete_eof_at_prompt_performs_no_write(monkeypatch):
    _setup(monkeypatch)
    monkeypatch.setattr(
        main, "build_braindump_delete", lambda cfg, braindump_id: (_ for _ in ()).throw(
            AssertionError("must not delete on EOF")
        )
    )

    result = runner.invoke(main.app, ["braindump", "delete", BD_ID], input="")

    assert result.exit_code == 2


def test_delete_json_without_yes_is_usage_exit(monkeypatch):
    _setup(monkeypatch)
    monkeypatch.setattr(
        main, "build_braindump_delete", lambda cfg, braindump_id: (_ for _ in ()).throw(
            AssertionError("must not delete without --yes in --json mode")
        )
    )

    result = runner.invoke(main.app, ["braindump", "delete", BD_ID, "--json"])

    assert result.exit_code == 2
    assert result.stdout == ""


def test_delete_json_with_yes_proceeds(monkeypatch):
    _setup(monkeypatch)
    monkeypatch.setattr(
        main, "build_braindump_delete",
        lambda cfg, braindump_id: Envelope.build(
            "nctl.braindump.delete.v1",
            BraindumpDeleteData(id=braindump_id, title="t", deleted=True, review_deleted=False),
        ),
    )

    result = runner.invoke(main.app, ["braindump", "delete", BD_ID, "--yes", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["deleted"] is True


# -- review ------------------------------------------------------------------------------------


def test_review_literal_summary(monkeypatch):
    _setup(monkeypatch)
    captured = {}

    def fake_review(cfg, braindump_id, *, summary=None, summary_file=None):
        captured.update(braindump_id=braindump_id, summary=summary, summary_file=summary_file)
        return Envelope.build(
            "nctl.braindump.review.v1",
            BraindumpReviewData(
                braindump=_record(review_present=True, alignment_review=_review_record()),
                action="created",
            ),
        )

    monkeypatch.setattr(main, "build_braindump_review", fake_review)

    result = runner.invoke(main.app, ["braindump", "review", BD_ID, "--summary", "looks good"])

    assert result.exit_code == 0
    assert captured == {"braindump_id": BD_ID, "summary": "looks good", "summary_file": None}
    assert "created review" in result.stdout


def test_review_file_mode(monkeypatch, tmp_path):
    _setup(monkeypatch)
    path = tmp_path / "summary.txt"
    path.write_text("file summary", encoding="utf-8")
    captured = {}

    def fake_review(cfg, braindump_id, *, summary=None, summary_file=None):
        captured.update(summary=summary, summary_file=summary_file)
        return Envelope.build(
            "nctl.braindump.review.v1",
            BraindumpReviewData(
                braindump=_record(review_present=True, alignment_review=_review_record()),
                action="replaced",
            ),
        )

    monkeypatch.setattr(main, "build_braindump_review", fake_review)

    result = runner.invoke(main.app, ["braindump", "review", BD_ID, "--file", str(path)])

    assert result.exit_code == 0
    assert captured["summary"] is None
    assert str(captured["summary_file"]) == str(path)
    assert "replaced review" in result.stdout


def test_review_neither_summary_nor_file_is_usage_exit(monkeypatch):
    _setup(monkeypatch)
    monkeypatch.setattr(
        main, "build_braindump_review",
        lambda cfg, braindump_id, **kw: _error_envelope(
            "nctl.braindump.review.v1", BraindumpReviewData(), "input_conflict"
        ),
    )

    result = runner.invoke(main.app, ["braindump", "review", BD_ID])

    assert result.exit_code == 2


def test_review_unknown_braindump_is_usage_exit(monkeypatch):
    _setup(monkeypatch)
    monkeypatch.setattr(
        main, "build_braindump_review",
        lambda cfg, braindump_id, **kw: _error_envelope(
            "nctl.braindump.review.v1", BraindumpReviewData(), "braindump_not_found"
        ),
    )

    result = runner.invoke(main.app, ["braindump", "review", BD_ID, "--summary", "s"])

    assert result.exit_code == 2


# -- review-delete -----------------------------------------------------------------------------


def test_review_delete_with_yes(monkeypatch):
    _setup(monkeypatch)
    monkeypatch.setattr(
        main, "build_braindump_review_delete",
        lambda cfg, braindump_id: Envelope.build(
            "nctl.braindump.review_delete.v1",
            BraindumpReviewDeleteData(braindump=_record(), deleted=True, review_id=REVIEW_ID),
        ),
    )

    result = runner.invoke(main.app, ["braindump", "review-delete", BD_ID, "--yes"])

    assert result.exit_code == 0
    assert "now Unreviewed" in result.stdout


def test_review_delete_no_op_when_absent(monkeypatch):
    _setup(monkeypatch)
    monkeypatch.setattr(
        main, "build_braindump_review_delete",
        lambda cfg, braindump_id: Envelope.build(
            "nctl.braindump.review_delete.v1",
            BraindumpReviewDeleteData(braindump=_record(), deleted=False, review_id=None),
        ),
    )

    result = runner.invoke(main.app, ["braindump", "review-delete", BD_ID, "--yes"])

    assert result.exit_code == 0
    assert "no change" in result.stdout


def test_review_delete_declined_prompt_performs_no_write(monkeypatch):
    _setup(monkeypatch)
    monkeypatch.setattr(
        main, "build_braindump_review_delete", lambda cfg, braindump_id: (_ for _ in ()).throw(
            AssertionError("must not delete without confirmation")
        )
    )

    result = runner.invoke(main.app, ["braindump", "review-delete", BD_ID], input="n\n")

    assert result.exit_code == 2


def test_review_delete_json_without_yes_is_usage_exit(monkeypatch):
    _setup(monkeypatch)
    monkeypatch.setattr(
        main, "build_braindump_review_delete", lambda cfg, braindump_id: (_ for _ in ()).throw(
            AssertionError("must not delete without --yes in --json mode")
        )
    )

    result = runner.invoke(main.app, ["braindump", "review-delete", BD_ID, "--json"])

    assert result.exit_code == 2
    assert result.stdout == ""
