import json

from typer.testing import CliRunner

import nctl_core.cli.main as main
from nctl_core.output import Envelope, EnvelopeError
from nctl_core.production_render import ProductionRenderData

runner = CliRunner()

SCHEMA = "nctl.render.production.v1"


def _ok_envelope() -> Envelope[ProductionRenderData]:
    data = ProductionRenderData(
        inventory={"all": {"vars": {}, "children": {}}},
        report={"generation_id": "gen-1", "summary": {"included": 1, "skipped": 0}},
        inventory_yaml="# inventory\nall:\n  vars: {}\n",
        report_json='{"generation_id": "gen-1"}\n',
    )
    return Envelope.build(SCHEMA, data, [])


def _failed_envelope() -> Envelope[ProductionRenderData]:
    return Envelope.build(SCHEMA, ProductionRenderData(), [EnvelopeError(code="nautobot_fetch_failed", message="boom")])


def test_render_production_default_prints_inventory_yaml_to_stdout(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_production_render", lambda cfg: _ok_envelope())

    result = runner.invoke(main.app, ["render", "production"])

    assert result.exit_code == 0
    assert "# inventory" in result.stdout


def test_render_production_json_prints_envelope(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_production_render", lambda cfg: _ok_envelope())

    result = runner.invoke(main.app, ["render", "production", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema"] == SCHEMA
    assert payload["data"]["report"]["summary"]["included"] == 1


def test_render_production_out_writes_artifacts_and_prints_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_production_render", lambda cfg: _ok_envelope())
    monkeypatch.setattr(main, "write_production_artifacts", lambda envelope, out: None)

    result = runner.invoke(main.app, ["render", "production", "--out", str(tmp_path)])

    assert result.exit_code == 0
    assert "included: 1" in result.stdout
    assert "# inventory" not in result.stdout


def test_render_production_out_reports_write_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_production_render", lambda cfg: _ok_envelope())
    monkeypatch.setattr(
        main,
        "write_production_artifacts",
        lambda envelope, out: EnvelopeError(code="ansible_inventory_invalid", message="bad inventory"),
    )

    result = runner.invoke(main.app, ["render", "production", "--out", str(tmp_path)])

    assert result.exit_code == 1
    assert "error [ansible_inventory_invalid]" in result.stdout


def test_render_production_exit_1_on_failure(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_production_render", lambda cfg: _failed_envelope())

    result = runner.invoke(main.app, ["render", "production"])

    assert result.exit_code == 1
    assert "error [nautobot_fetch_failed]" in result.stdout
