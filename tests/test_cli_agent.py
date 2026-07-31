import json

from typer.testing import CliRunner

import nctl_core.cli.main as main
from nctl_core.agent import AGENT_RUN_SCHEMA, AGENT_STATUS_SCHEMA, AgentStatusData, AgentTaskData
from nctl_core.output import Envelope, EnvelopeError

runner = CliRunner()


def _envelope(code=None):
    errors = [EnvelopeError(code=code, message="boom")] if code else []
    return Envelope.build(AGENT_STATUS_SCHEMA, AgentStatusData(node_slug="agpc", reachable=not code), errors)


def test_agent_status_passes_host_and_json(monkeypatch):
    captured = {}
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_agent_status", lambda cfg, host: captured.setdefault("envelope", _envelope()))
    result = runner.invoke(main.app, ["agent", "status", "agpc", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output)["schema"] == AGENT_STATUS_SCHEMA


def test_agent_status_unknown_host_is_usage(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_agent_status", lambda cfg, host: _envelope("unknown_host"))
    assert runner.invoke(main.app, ["agent", "status", "nope"]).exit_code == 2


def test_agent_run_passes_prompt_and_emits_json(monkeypatch):
    captured = {}
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_agent_run", lambda cfg, host, prompt: captured.setdefault("envelope", Envelope.build(AGENT_RUN_SCHEMA, AgentTaskData(node_slug=host, session_id="ses_new", reply=prompt))))
    result = runner.invoke(main.app, ["agent", "run", "agpc", "--prompt", "inspect", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output)["data"]["reply"] == "inspect"
