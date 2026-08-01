import json

from typer.testing import CliRunner

import nctl_core.cli.main as main
from nctl_core.output import Envelope, EnvelopeError
from nctl_core.workspaces_render import WorkspaceRow, WorkspacesData

runner = CliRunner()


def _ok_envelope() -> Envelope[WorkspacesData]:
    data = WorkspacesData(
        generated_at="2026-08-01T13:00:00+00:00",
        rows=[
            WorkspaceRow(
                slug="pj-voxel3dprint", name="pj-voxel3dprint", node="agpc",
                desired_presence="present", presence="present", identity="matched",
                activity_class="active_development", activity_reasons={"ahead": 5, "behind": 0, "dirty": False},
                freshness="fresh", checked_at="2026-08-01T12:30:00+00:00", gap_codes=[],
            ),
        ],
        summary={"converged": 1},
    )
    return Envelope.build("nctl.workspaces.v1", data, [])


def _failed_envelope() -> Envelope[WorkspacesData]:
    return Envelope.build("nctl.workspaces.v1", WorkspacesData(), [EnvelopeError(code="nautobot_fetch_failed", message="boom")])


def test_workspaces_default_prints_text_to_stdout(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_workspaces", lambda cfg, host=None: _ok_envelope())

    result = runner.invoke(main.app, ["workspaces"])

    assert result.exit_code == 0
    assert "pj-voxel3dprint @agpc" in result.stdout
    assert "presence=present" in result.stdout
    assert "identity=matched" in result.stdout
    assert "activity=active_development" in result.stdout
    assert "freshness=fresh" in result.stdout
    assert "summary: converged=1" in result.stdout


def test_workspaces_json_prints_envelope(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_workspaces", lambda cfg, host=None: _ok_envelope())

    result = runner.invoke(main.app, ["workspaces", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema"] == "nctl.workspaces.v1"
    assert payload["data"]["rows"][0]["slug"] == "pj-voxel3dprint"


def test_workspaces_passes_host_filter_through(monkeypatch):
    captured = {}

    def fake_build_workspaces(cfg, host=None):
        captured["host"] = host
        return _ok_envelope()

    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_workspaces", fake_build_workspaces)

    result = runner.invoke(main.app, ["workspaces", "--host", "agpc"])

    assert result.exit_code == 0
    assert captured == {"host": "agpc"}


def test_workspaces_exit_1_on_failure(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_workspaces", lambda cfg, host=None: _failed_envelope())

    result = runner.invoke(main.app, ["workspaces"])

    assert result.exit_code == 1
    assert "error [nautobot_fetch_failed]" in result.stdout
