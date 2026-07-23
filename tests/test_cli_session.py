import json

from typer.testing import CliRunner

import nctl_core.cli.main as main
from nctl_core.output import Envelope, EnvelopeError
from nctl_core.session import SessionNewData

runner = CliRunner()


def _ok_envelope() -> Envelope[SessionNewData]:
    data = SessionNewData(task_name="brainforge", topic="dns-fix", slug="2026-07-23_dns-fix_aaaa", path="/repo/.local/workspace/brainforge/2026-07-23_dns-fix_aaaa")
    return Envelope.build("nctl.session.new.v1", data, [])


def _invalid_envelope() -> Envelope[SessionNewData]:
    data = SessionNewData(task_name="Bad Name!", topic=None, slug="", path="")
    return Envelope.build("nctl.session.new.v1", data, [EnvelopeError(code="invalid_task_name", message="boom")])


def test_session_new_prints_path_and_passes_args_through(monkeypatch):
    captured = {}

    def fake_build_session_new(cfg, task_name, topic=None):
        captured["task_name"] = task_name
        captured["topic"] = topic
        return _ok_envelope()

    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_session_new", fake_build_session_new)

    result = runner.invoke(main.app, ["session", "new", "brainforge", "--topic", "dns-fix"])

    assert result.exit_code == 0
    assert captured == {"task_name": "brainforge", "topic": "dns-fix"}
    assert result.output.strip() == "/repo/.local/workspace/brainforge/2026-07-23_dns-fix_aaaa"


def test_session_new_json_output(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_session_new", lambda cfg, task_name, topic=None: _ok_envelope())

    result = runner.invoke(main.app, ["session", "new", "brainforge", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["data"]["slug"] == "2026-07-23_dns-fix_aaaa"


def test_session_new_invalid_task_name_exits_usage(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_session_new", lambda cfg, task_name, topic=None: _invalid_envelope())

    result = runner.invoke(main.app, ["session", "new", "Bad Name!"])

    assert result.exit_code == 2
