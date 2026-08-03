"""CLI-only tests for `nctl workflow-episode *` (Step 1: list/show).

Mocks the `nctl_core.cli.main.build_workflow_episode_*` core boundary, per the `test_cli_braindump.py`
convention of mocking at the CLI's own import site.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from typer.testing import CliRunner

import nctl_core.cli.main as main
from nctl_core.output import Envelope, EnvelopeError
from nctl_core.workflow_episode import (
    WorkflowEpisodeCreateData,
    WorkflowEpisodeListData,
    WorkflowEpisodeListItem,
    WorkflowEpisodeRecord,
    WorkflowEpisodeShowData,
    WorkflowEpisodeWriteData,
)

runner = CliRunner()
WE_ID = "11111111-1111-1111-1111-111111111111"
T0 = datetime(2026, 8, 4, tzinfo=timezone.utc)


def _setup(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: object())


def _list_item(**overrides) -> WorkflowEpisodeListItem:
    fields = dict(id=WE_ID, title="t", status="candidate", created=T0, last_updated=T0)
    fields.update(overrides)
    return WorkflowEpisodeListItem(**fields)


def _record(**overrides) -> WorkflowEpisodeRecord:
    fields = dict(id=WE_ID, title="t", status="candidate", raw_data={}, created=T0, last_updated=T0)
    fields.update(overrides)
    return WorkflowEpisodeRecord(**fields)


def test_list_prints_text(monkeypatch):
    _setup(monkeypatch)
    captured = {}

    def fake_list(cfg, **kwargs):
        captured.update(kwargs)
        return Envelope.build("nctl.workflow_episode.list.v1", WorkflowEpisodeListData(items=[_list_item()], count=1))

    monkeypatch.setattr(main, "build_workflow_episode_list", fake_list)

    result = runner.invoke(main.app, ["workflow-episode", "list"])

    assert result.exit_code == 0
    assert WE_ID in result.stdout
    assert captured["statuses"] == frozenset({"candidate", "selected"})


def test_list_status_option_repeatable(monkeypatch):
    _setup(monkeypatch)
    captured = {}

    def fake_list(cfg, **kwargs):
        captured.update(kwargs)
        return Envelope.build("nctl.workflow_episode.list.v1", WorkflowEpisodeListData())

    monkeypatch.setattr(main, "build_workflow_episode_list", fake_list)

    result = runner.invoke(main.app, ["workflow-episode", "list", "--status", "resolved", "--status", "dismissed"])

    assert result.exit_code == 0
    assert captured["statuses"] == frozenset({"resolved", "dismissed"})


def test_list_all_overrides_status(monkeypatch):
    _setup(monkeypatch)
    captured = {}

    def fake_list(cfg, **kwargs):
        captured.update(kwargs)
        return Envelope.build("nctl.workflow_episode.list.v1", WorkflowEpisodeListData())

    monkeypatch.setattr(main, "build_workflow_episode_list", fake_list)

    result = runner.invoke(main.app, ["workflow-episode", "list", "--status", "resolved", "--all"])

    assert result.exit_code == 0
    assert captured["statuses"] is None


def test_list_json_matches_envelope(monkeypatch):
    _setup(monkeypatch)
    envelope = Envelope.build("nctl.workflow_episode.list.v1", WorkflowEpisodeListData(items=[_list_item()], count=1))
    monkeypatch.setattr(main, "build_workflow_episode_list", lambda cfg, **kwargs: envelope)

    result = runner.invoke(main.app, ["workflow-episode", "list", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema"] == "nctl.workflow_episode.list.v1"
    assert payload["data"]["count"] == 1


def test_show_prints_text_with_sections(monkeypatch):
    _setup(monkeypatch)
    record = _record(raw_data={"report": {"summary": "s"}, "assessment": {}, "references": {}, "resolution": {}})
    monkeypatch.setattr(main, "build_workflow_episode_show", lambda cfg, episode_id: Envelope.build("nctl.workflow_episode.show.v1", WorkflowEpisodeShowData(episode=record)))

    result = runner.invoke(main.app, ["workflow-episode", "show", WE_ID])

    assert result.exit_code == 0
    assert WE_ID in result.stdout
    assert "report:" in result.stdout
    assert "assessment:" in result.stdout


def test_show_error_maps_to_usage_exit_code(monkeypatch):
    _setup(monkeypatch)
    envelope = Envelope.build(
        "nctl.workflow_episode.show.v1", WorkflowEpisodeShowData(),
        [EnvelopeError(code="workflow_episode_not_found", message="no such episode")],
    )
    monkeypatch.setattr(main, "build_workflow_episode_show", lambda cfg, episode_id: envelope)

    result = runner.invoke(main.app, ["workflow-episode", "show", WE_ID])

    assert result.exit_code == 2


def test_show_connection_error_maps_to_failure_exit_code(monkeypatch):
    _setup(monkeypatch)
    envelope = Envelope.build(
        "nctl.workflow_episode.show.v1", WorkflowEpisodeShowData(),
        [EnvelopeError(code="nautobot_connection_error", message="cannot reach")],
    )
    monkeypatch.setattr(main, "build_workflow_episode_show", lambda cfg, episode_id: envelope)

    result = runner.invoke(main.app, ["workflow-episode", "show", WE_ID])

    assert result.exit_code == 1


# -- create ------------------------------------------------------------------------------------


def test_create_prints_text(monkeypatch):
    _setup(monkeypatch)
    captured = {}

    def fake_create(cfg, **kwargs):
        captured.update(kwargs)
        return Envelope.build("nctl.workflow_episode.create.v1", WorkflowEpisodeCreateData(episode=_record(), changed=True))

    monkeypatch.setattr(main, "build_workflow_episode_create", fake_create)

    result = runner.invoke(main.app, ["workflow-episode", "create", "--title", "My title", "--raw-data", '{"report": {"summary": "s"}}'])

    assert result.exit_code == 0
    assert "created workflow episode" in result.stdout
    assert captured["title"] == "My title"
    assert captured["raw_data"] == '{"report": {"summary": "s"}}'


def test_create_without_raw_data(monkeypatch):
    _setup(monkeypatch)
    captured = {}

    def fake_create(cfg, **kwargs):
        captured.update(kwargs)
        return Envelope.build("nctl.workflow_episode.create.v1", WorkflowEpisodeCreateData(episode=_record(), changed=True))

    monkeypatch.setattr(main, "build_workflow_episode_create", fake_create)

    result = runner.invoke(main.app, ["workflow-episode", "create", "--title", "My title"])

    assert result.exit_code == 0
    assert captured["raw_data"] is None
    assert captured["raw_data_file"] is None


def test_create_error_maps_to_usage_exit_code(monkeypatch):
    _setup(monkeypatch)
    envelope = Envelope.build(
        "nctl.workflow_episode.create.v1", WorkflowEpisodeCreateData(),
        [EnvelopeError(code="invalid_json", message="bad json")],
    )
    monkeypatch.setattr(main, "build_workflow_episode_create", lambda cfg, **kwargs: envelope)

    result = runner.invoke(main.app, ["workflow-episode", "create", "--title", "t", "--raw-data", "not json"])

    assert result.exit_code == 2


# -- write -------------------------------------------------------------------------------------


def test_write_prints_text(monkeypatch):
    _setup(monkeypatch)
    captured = {}

    def fake_write(cfg, episode_id, namespace, **kwargs):
        captured["episode_id"] = episode_id
        captured["namespace"] = namespace
        captured.update(kwargs)
        return Envelope.build(
            "nctl.workflow_episode.write.v1",
            WorkflowEpisodeWriteData(episode=_record(raw_data={"assessment": {"verdict": "promote"}}), namespace="assessment", changed=True),
        )

    monkeypatch.setattr(main, "build_workflow_episode_write", fake_write)

    result = runner.invoke(main.app, ["workflow-episode", "write", WE_ID, "assessment", "--data", '{"verdict": "promote"}'])

    assert result.exit_code == 0
    assert "wrote assessment" in result.stdout
    assert captured["episode_id"] == WE_ID
    assert captured["namespace"] == "assessment"
    assert captured["data"] == '{"verdict": "promote"}'


def test_write_rejects_unknown_namespace(monkeypatch):
    _setup(monkeypatch)
    result = runner.invoke(main.app, ["workflow-episode", "write", WE_ID, "bogus", "--data", "{}"])
    assert result.exit_code == 2


def test_write_error_maps_to_usage_exit_code(monkeypatch):
    _setup(monkeypatch)
    envelope = Envelope.build(
        "nctl.workflow_episode.write.v1", WorkflowEpisodeWriteData(),
        [EnvelopeError(code="workflow_episode_not_found", message="no such episode")],
    )
    monkeypatch.setattr(main, "build_workflow_episode_write", lambda cfg, episode_id, namespace, **kwargs: envelope)

    result = runner.invoke(main.app, ["workflow-episode", "write", WE_ID, "assessment", "--data", "{}"])

    assert result.exit_code == 2
