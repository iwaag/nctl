import json
from types import SimpleNamespace

from typer.testing import CliRunner

import nctl_core.cli.main as main
from nctl_core.artifacts import OperationArtifacts
from nctl_core.events import OperationLog
from nctl_core.ops_render import build_ops_list, build_ops_show

runner = CliRunner()


def _cfg_for(log_dir):
    return SimpleNamespace(events=SimpleNamespace(resolved_log_dir=lambda: log_dir))


def _seed_operation(log_dir, message="converged"):
    log = OperationLog.start("reconcile", log_dir)
    log.emit("finished", message, ok=True)
    OperationArtifacts.create(log_dir, log.operation_id).write_json("plan.json", {})
    return log.operation_id


def test_ops_list_text_and_exit_code(tmp_path, monkeypatch):
    operation_id = _seed_operation(tmp_path)
    monkeypatch.setattr(main, "_load_config", lambda path: _cfg_for(tmp_path))

    result = runner.invoke(main.app, ["ops", "list"])

    assert result.exit_code == 0
    assert operation_id in result.stdout
    assert "finished" in result.stdout
    assert "converged" in result.stdout


def test_ops_list_json_envelope(tmp_path, monkeypatch):
    operation_id = _seed_operation(tmp_path)
    monkeypatch.setattr(main, "_load_config", lambda path: _cfg_for(tmp_path))

    result = runner.invoke(main.app, ["ops", "list", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema"] == "nctl.ops.list.v1"
    assert payload["data"]["operations"][0]["operation_id"] == operation_id
    assert payload["data"]["operations"][0]["state"] == "finished"


def test_ops_list_limit_passes_through(tmp_path, monkeypatch):
    for _ in range(3):
        _seed_operation(tmp_path)
    monkeypatch.setattr(main, "_load_config", lambda path: _cfg_for(tmp_path))

    result = runner.invoke(main.app, ["ops", "list", "--limit", "2", "--json"])

    assert len(json.loads(result.stdout)["data"]["operations"]) == 2


def test_ops_show_text_includes_artifacts_and_events(tmp_path, monkeypatch):
    operation_id = _seed_operation(tmp_path)
    monkeypatch.setattr(main, "_load_config", lambda path: _cfg_for(tmp_path))

    result = runner.invoke(main.app, ["ops", "show", operation_id])

    assert result.exit_code == 0
    assert f"operation_id: {operation_id}" in result.stdout
    assert "plan.json" in result.stdout
    assert "finished: converged" in result.stdout


def test_ops_show_after_seq_filters_events(tmp_path, monkeypatch):
    operation_id = _seed_operation(tmp_path)
    monkeypatch.setattr(main, "_load_config", lambda path: _cfg_for(tmp_path))

    result = runner.invoke(main.app, ["ops", "show", operation_id, "--after-seq", "0", "--json"])

    payload = json.loads(result.stdout)
    assert payload["schema"] == "nctl.ops.show.v1"
    assert [e["seq"] for e in payload["data"]["events"]] == [1]


def test_ops_show_unknown_id_exits_usage(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: _cfg_for(tmp_path))

    result = runner.invoke(main.app, ["ops", "show", "01JZZZZZZZZZZZZZZZZZZZZZZZ"])

    assert result.exit_code == 2
    assert "unknown_operation" in result.stdout


def test_ops_show_malformed_id_exits_usage(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: _cfg_for(tmp_path))

    result = runner.invoke(main.app, ["ops", "show", "../../etc/passwd"])

    assert result.exit_code == 2
    assert "malformed_operation_id" in result.stdout


def test_build_ops_show_envelope_ok_flag(tmp_path):
    cfg = _cfg_for(tmp_path)
    operation_id = _seed_operation(tmp_path)
    assert build_ops_show(cfg, operation_id).ok is True
    assert build_ops_show(cfg, "01JZZZZZZZZZZZZZZZZZZZZZZZ").ok is False
    assert build_ops_list(cfg).ok is True
