import json
from pathlib import Path

from typer.testing import CliRunner

import nctl_core.cli.main as main
from nctl_core.dashboard_render import DashboardData, StatusPushData
from nctl_core.output import Envelope, EnvelopeError

runner = CliRunner()


def _ok_envelope() -> Envelope[DashboardData]:
    data = DashboardData(
        html_path="/dash/index.html",
        drift_json_path="/dash/drift.json",
        generated_at="2026-07-16T12:00:00+00:00",
        summary={"converged": 3, "unknown": 2},
        severity_summary={"error": 2, "warning": 9, "info": 0},
        status_push=StatusPushData(),
    )
    return Envelope.build("nctl.dashboard.v1", data, [])


def _failed_envelope() -> Envelope[DashboardData]:
    return Envelope.build(
        "nctl.dashboard.v1", DashboardData(), [EnvelopeError(code="nautobot_fetch_failed", message="boom")]
    )


def test_dashboard_default_prints_text(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_dashboard", lambda cfg, out_dir=None, from_file=None, push=True: _ok_envelope())

    result = runner.invoke(main.app, ["dashboard"])

    assert result.exit_code == 0
    assert "dashboard: /dash/index.html" in result.stdout
    assert "summary: converged=3 unknown=2" in result.stdout
    assert "status push: skipped" in result.stdout


def test_dashboard_json_prints_envelope(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_dashboard", lambda cfg, out_dir=None, from_file=None, push=True: _ok_envelope())

    result = runner.invoke(main.app, ["dashboard", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema"] == "nctl.dashboard.v1"
    assert payload["data"]["html_path"] == "/dash/index.html"


def test_dashboard_passes_options_through(monkeypatch):
    captured = {}

    def fake_build_dashboard(cfg, out_dir=None, from_file=None, push=True):
        captured.update(out_dir=out_dir, from_file=from_file, push=push)
        return _ok_envelope()

    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_dashboard", fake_build_dashboard)

    result = runner.invoke(
        main.app, ["dashboard", "--out", "/tmp/dash", "--from", "/tmp/saved.json", "--no-push"]
    )

    assert result.exit_code == 0
    assert captured == {"out_dir": Path("/tmp/dash"), "from_file": Path("/tmp/saved.json"), "push": False}


def test_dashboard_exit_1_on_failure(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_dashboard", lambda cfg, out_dir=None, from_file=None, push=True: _failed_envelope())

    result = runner.invoke(main.app, ["dashboard"])

    assert result.exit_code == 1
    assert "error [nautobot_fetch_failed]" in result.stdout
