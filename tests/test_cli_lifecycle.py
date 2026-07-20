import json

from typer.testing import CliRunner

import nctl_core.cli.main as main
from nctl_core.lifecycle import LifecycleData
from nctl_core.output import Envelope, EnvelopeError

runner = CliRunner()


def _changed_envelope() -> Envelope[LifecycleData]:
    data = LifecycleData(
        node_id="n1", node_slug="agpc", previous_state="planned", requested_state="active",
        current_state="active", changed=True,
    )
    return Envelope.build("nctl.lifecycle.v1", data, [])


def _unchanged_envelope() -> Envelope[LifecycleData]:
    data = LifecycleData(
        node_id="n1", node_slug="agpc", previous_state="active", requested_state="active",
        current_state="active", changed=False,
    )
    return Envelope.build("nctl.lifecycle.v1", data, [])


def _failed_envelope(code: str) -> Envelope[LifecycleData]:
    data = LifecycleData(
        node_id="", node_slug="agpc", previous_state="", requested_state="active",
        current_state="", changed=False,
    )
    return Envelope.build("nctl.lifecycle.v1", data, [EnvelopeError(code=code, message="boom")])


def test_lifecycle_default_prints_text_and_passes_args_through(monkeypatch):
    captured = {}

    def fake_build_lifecycle(cfg, node, state):
        captured["node"] = node
        captured["state"] = state
        return _changed_envelope()

    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_lifecycle", fake_build_lifecycle)

    result = runner.invoke(main.app, ["lifecycle", "agpc", "active"])

    assert result.exit_code == 0
    assert captured == {"node": "agpc", "state": "active"}
    assert "agpc: planned -> active" in result.stdout


def test_lifecycle_json_prints_envelope(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_lifecycle", lambda cfg, node, state: _changed_envelope())

    result = runner.invoke(main.app, ["lifecycle", "agpc", "active", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema"] == "nctl.lifecycle.v1"
    assert payload["data"]["changed"] is True


def test_lifecycle_idempotent_no_change_text(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_lifecycle", lambda cfg, node, state: _unchanged_envelope())

    result = runner.invoke(main.app, ["lifecycle", "agpc", "active"])

    assert result.exit_code == 0
    assert "agpc: already active (no change)" in result.stdout


def test_lifecycle_invalid_state_is_usage_exit(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_lifecycle", lambda cfg, node, state: _failed_envelope("invalid_lifecycle"))

    result = runner.invoke(main.app, ["lifecycle", "agpc", "bogus"])

    assert result.exit_code == 2


def test_lifecycle_unknown_node_is_usage_exit(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_lifecycle", lambda cfg, node, state: _failed_envelope("unknown_node"))

    result = runner.invoke(main.app, ["lifecycle", "no-such-node", "active"])

    assert result.exit_code == 2


def test_lifecycle_rejection_is_failure_exit_not_usage(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(
        main, "build_lifecycle", lambda cfg, node, state: _failed_envelope("lifecycle_update_rejected")
    )

    result = runner.invoke(main.app, ["lifecycle", "agpc", "active"])

    assert result.exit_code == 1
