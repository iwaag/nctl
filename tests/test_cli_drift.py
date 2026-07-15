import json

from typer.testing import CliRunner

import nctl_core.cli.main as main
from nctl_core.drift.model import Severity, Status, Target
from nctl_core.drift.engine import TargetStatus
from nctl_core.drift_render import DriftData, DriftSourcesData
from nctl_core.output import Envelope, EnvelopeError

runner = CliRunner()


def _ok_envelope() -> Envelope[DriftData]:
    data = DriftData(
        generated_at="2026-07-15T12:00:00+00:00",
        summary={"converged": 1, "unknown": 1},
        severity_summary={"error": 1, "warning": 0, "info": 0},
        targets=[
            TargetStatus(target=Target(kind="node", slug="agok", name="agok", id="n1"), status=Status.CONVERGED, diffs=[]),
        ],
        sources=DriftSourcesData(fetched_at="2026-07-15T12:00:00+00:00", observed_dump_count=1),
    )
    return Envelope.build("nctl.drift.v1", data, [])


def _failed_envelope() -> Envelope[DriftData]:
    return Envelope.build("nctl.drift.v1", DriftData(), [EnvelopeError(code="nautobot_fetch_failed", message="boom")])


def test_drift_default_prints_text_to_stdout(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_drift", lambda cfg, host=None, service=None: _ok_envelope())

    result = runner.invoke(main.app, ["drift"])

    assert result.exit_code == 0
    assert "agok  converged  0 diff(s)" in result.stdout
    assert "summary: converged=1 unknown=1" in result.stdout


def test_drift_json_prints_envelope(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_drift", lambda cfg, host=None, service=None: _ok_envelope())

    result = runner.invoke(main.app, ["drift", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema"] == "nctl.drift.v1"
    assert payload["data"]["summary"] == {"converged": 1, "unknown": 1}


def test_drift_passes_host_and_service_filters_through(monkeypatch):
    captured = {}

    def fake_build_drift(cfg, host=None, service=None):
        captured["host"] = host
        captured["service"] = service
        return _ok_envelope()

    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_drift", fake_build_drift)

    result = runner.invoke(main.app, ["drift", "--host", "agok", "--service", "web"])

    assert result.exit_code == 0
    assert captured == {"host": "agok", "service": "web"}


def test_drift_exit_1_on_failure(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_drift", lambda cfg, host=None, service=None: _failed_envelope())

    result = runner.invoke(main.app, ["drift"])

    assert result.exit_code == 1
    assert "error [nautobot_fetch_failed]" in result.stdout
