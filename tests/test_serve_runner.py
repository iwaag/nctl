from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from nctl_core.config import Config
from nctl_core.output import Envelope, EnvelopeError
from nctl_core.serve import runner as runner_module
from nctl_core.serve.runner import (
    DashboardParams,
    DriftParams,
    OperationRunner,
    ReconcileParams,
    RenderProductionParams,
    RunnerError,
    is_mutating,
    parse_params,
)


def _config(tmp_path: Path) -> Config:
    return Config.model_validate(
        {
            "nautobot": {"url": "http://nautobot.test"},
            "inventory": {"dumps_dir": tmp_path / "dumps"},
            "events": {"log_dir": tmp_path / "events"},
            "ansible": {"playbook_dir": tmp_path / "ansible", "inventory": "inventory.yml"},
            "dashboard": {"out_dir": tmp_path / "dashboard"},
            "reconcile": {"lock_path": tmp_path / "reconcile.lock"},
            "source_path": tmp_path / "nctl.toml",
        }
    )


def _wait_until_finished(handle, timeout=2.0) -> None:
    deadline = time.monotonic() + timeout
    while handle.state != "finished":
        if time.monotonic() > deadline:
            raise AssertionError(f"operation {handle.operation_id} did not finish in time")
        time.sleep(0.005)


# --- param parsing -----------------------------------------------------------------


def test_parse_params_rejects_unknown_op():
    with pytest.raises(RunnerError) as excinfo:
        parse_params("bogus", {})
    assert excinfo.value.code == "unsupported_op"


def test_parse_params_rejects_extra_fields():
    with pytest.raises(RunnerError) as excinfo:
        parse_params("drift", {"nope": True})
    assert excinfo.value.code == "validation_error"


def test_parse_params_rejects_out_of_range_max_rounds():
    with pytest.raises(RunnerError):
        parse_params("reconcile", {"max_rounds": 0})


def test_parse_params_accepts_documented_fields():
    assert parse_params("drift", {"host": "agpc"}) == DriftParams(host="agpc")
    assert parse_params("dashboard", {"no_push": True}) == DashboardParams(no_push=True)
    assert parse_params("render.production", {"write": True}) == RenderProductionParams(write=True)
    assert parse_params("reconcile", {"yes": True, "max_rounds": 2}) == ReconcileParams(yes=True, max_rounds=2)


# --- mutating classification --------------------------------------------------------


def test_is_mutating_rules():
    assert is_mutating("dashboard", DashboardParams()) is True
    assert is_mutating("dashboard", DashboardParams(no_push=True)) is True
    assert is_mutating("drift", DriftParams()) is False
    assert is_mutating("render.dnsmasq", parse_params("render.dnsmasq", {})) is False
    assert is_mutating("render.production", RenderProductionParams(write=False)) is False
    assert is_mutating("render.production", RenderProductionParams(write=True)) is True
    assert is_mutating("reconcile", ReconcileParams(yes=False)) is False
    assert is_mutating("reconcile", ReconcileParams(yes=True)) is True


# --- gate --------------------------------------------------------------------------


def test_gate_rejects_second_writer_and_reports_holder():
    gate = runner_module._Gate()
    gate.enter("op-1", mutating=True)
    with pytest.raises(RunnerError) as excinfo:
        gate.enter("op-2", mutating=True)
    assert excinfo.value.code == "operation_conflict"
    assert excinfo.value.detail == {"running_operation_id": "op-1"}
    gate.leave("op-1", mutating=True)
    gate.enter("op-2", mutating=True)  # no longer blocked


def test_gate_allows_concurrent_readers_but_blocks_writer():
    gate = runner_module._Gate()
    gate.enter("reader-1", mutating=False)
    gate.enter("reader-2", mutating=False)  # readers stack freely
    with pytest.raises(RunnerError):
        gate.enter("writer", mutating=True)
    gate.leave("reader-1", mutating=False)
    with pytest.raises(RunnerError):
        gate.enter("writer", mutating=True)  # reader-2 still active
    gate.leave("reader-2", mutating=False)
    gate.enter("writer", mutating=True)


def test_gate_writer_blocks_new_readers():
    gate = runner_module._Gate()
    gate.enter("writer", mutating=True)
    with pytest.raises(RunnerError) as excinfo:
        gate.enter("reader", mutating=False)
    assert excinfo.value.detail == {"running_operation_id": "writer"}


# --- OperationRunner: wrapped (drift/dashboard/render.*) ops ------------------------


def test_submit_wrapped_op_writes_events_and_public_result(tmp_path, monkeypatch):
    cfg = _config(tmp_path)

    def _fake_build_drift(cfg, *, host=None, service=None):
        from nctl_core.drift_render import DriftData

        return Envelope.build("nctl.drift.v1", DriftData())

    monkeypatch.setattr(runner_module, "build_drift", _fake_build_drift)

    op_runner = OperationRunner(cfg)
    handle = op_runner.submit("drift", {})
    assert handle.op == "drift"
    assert handle.mutating is False
    _wait_until_finished(handle)
    assert handle.error is None

    log_dir = cfg.events.resolved_log_dir()
    jsonl = log_dir / f"{handle.operation_id}.jsonl"
    lines = [json.loads(line) for line in jsonl.read_text().splitlines()]
    assert [record["event"] for record in lines] == ["started", "finished"]
    assert lines[0]["op"] == "drift"
    assert lines[-1]["data"]["ok"] is True

    result_path = log_dir / handle.operation_id / "result.json"
    assert result_path.stat().st_mode & 0o777 == 0o644
    payload = json.loads(result_path.read_text())
    assert payload["schema"] == "nctl.drift.v1"
    assert payload["ok"] is True


def test_submit_wrapped_op_records_failure_without_crashing(tmp_path, monkeypatch):
    cfg = _config(tmp_path)

    def _failing_build_drift(cfg, *, host=None, service=None):
        from nctl_core.drift_render import DriftData

        return Envelope.build("nctl.drift.v1", DriftData(), [EnvelopeError(code="boom", message="nope")])

    monkeypatch.setattr(runner_module, "build_drift", _failing_build_drift)

    op_runner = OperationRunner(cfg)
    handle = op_runner.submit("drift", {})
    _wait_until_finished(handle)

    result_path = cfg.events.resolved_log_dir() / handle.operation_id / "result.json"
    payload = json.loads(result_path.read_text())
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "boom"


def test_submit_render_production_write_flag_selects_canonical_dir(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    written_dirs = []

    def _fake_build_production_render(cfg):
        from nctl_core.production_render import ProductionRenderData

        return Envelope.build("nctl.render.production.v1", ProductionRenderData())

    def _fake_write_production_artifacts(envelope, out_dir):
        written_dirs.append(out_dir)
        return None

    monkeypatch.setattr(runner_module, "build_production_render", _fake_build_production_render)
    monkeypatch.setattr(runner_module, "write_production_artifacts", _fake_write_production_artifacts)

    op_runner = OperationRunner(cfg)
    handle = op_runner.submit("render.production", {"write": True})
    _wait_until_finished(handle)

    assert written_dirs == [cfg.ansible.resolved_inventory(cfg.source_path.parent).parent]


def test_submit_render_production_without_write_does_not_touch_disk(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    called = []

    def _fake_build_production_render(cfg):
        from nctl_core.production_render import ProductionRenderData

        return Envelope.build("nctl.render.production.v1", ProductionRenderData())

    def _unexpected_write(*args, **kwargs):
        called.append(True)
        raise AssertionError("write_production_artifacts should not be called")

    monkeypatch.setattr(runner_module, "build_production_render", _fake_build_production_render)
    monkeypatch.setattr(runner_module, "write_production_artifacts", _unexpected_write)

    op_runner = OperationRunner(cfg)
    handle = op_runner.submit("render.production", {})
    _wait_until_finished(handle)
    assert called == []


# --- OperationRunner: single-flight / gate integration ------------------------------


def test_submit_second_mutating_op_conflicts_with_running_operation_id(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    release = threading.Event()
    started = threading.Event()

    def _slow_run_reconcile(cfg, *, host=None, apply_changes=False, max_rounds=None, operation_id=None, **_kw):
        started.set()
        release.wait(timeout=2)
        from nctl_core.reconcile.model import PlanScope
        from nctl_core.reconcile.executor import ReconcileData

        data = ReconcileData(operation_id=operation_id, mode="apply", scope=PlanScope(kind="cluster"), event_log_path="")
        return Envelope.build("nctl.reconcile.v1", data)

    monkeypatch.setattr(runner_module, "run_reconcile", _slow_run_reconcile)

    op_runner = OperationRunner(cfg)
    first = op_runner.submit("reconcile", {"yes": True})
    assert started.wait(timeout=2)

    with pytest.raises(RunnerError) as excinfo:
        op_runner.submit("reconcile", {"yes": True})
    assert excinfo.value.code == "operation_conflict"
    assert excinfo.value.detail == {"running_operation_id": first.operation_id}

    with pytest.raises(RunnerError) as excinfo:
        op_runner.submit("drift", {})
    assert excinfo.value.detail == {"running_operation_id": first.operation_id}

    release.set()
    _wait_until_finished(first)

    # the gate is free again
    second = op_runner.submit("drift", {})
    _wait_until_finished(second)
