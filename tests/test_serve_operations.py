"""ASGI-level tests for `POST /api/v1/operations` (Phase 5 Step 3)."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import httpx

from nctl_core.config import Config
from nctl_core.output import Envelope
from nctl_core.serve import app as app_module
from nctl_core.serve.app import create_app


def _config(tmp_path: Path, **overrides) -> Config:
    base = {
        "nautobot": {"url": "http://nautobot.test"},
        "inventory": {"dumps_dir": tmp_path / "dumps"},
        "events": {"log_dir": tmp_path / "events"},
        "ansible": {"playbook_dir": tmp_path / "ansible", "inventory": "inventory.yml"},
        "dashboard": {"out_dir": tmp_path / "dashboard"},
        "reconcile": {"lock_path": tmp_path / "reconcile.lock"},
        "serve": {"auth": "none"},
        "source_path": tmp_path / "nctl.toml",
    }
    base.update(overrides)
    return Config.model_validate(base)


def _request(app, method, path, **kwargs):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(run())


def _poll_finished(app, operation_id, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = _request(app, "GET", f"/api/v1/operations/{operation_id}")
        if response.status_code == 200 and response.json()["operation"]["state"] == "finished":
            return response
        time.sleep(0.01)
    raise AssertionError(f"operation {operation_id} did not reach 'finished' via the API in time")


def test_create_operation_requires_auth(tmp_path):
    cfg = _config(tmp_path, serve={"auth": "token", "token_env": "NCTL_SERVE_TOKEN_OPS_TEST"})
    import os

    os.environ["NCTL_SERVE_TOKEN_OPS_TEST"] = "s3cr3t"
    try:
        app = create_app(cfg)
        response = _request(app, "POST", "/api/v1/operations", json={"op": "drift"})
        assert response.status_code == 401
    finally:
        del os.environ["NCTL_SERVE_TOKEN_OPS_TEST"]


def test_create_operation_rejects_malformed_body(tmp_path):
    app = create_app(_config(tmp_path))
    missing_op = _request(app, "POST", "/api/v1/operations", json={})
    assert missing_op.status_code == 422

    bad_params = _request(app, "POST", "/api/v1/operations", json={"op": "drift", "params": "nope"})
    assert bad_params.status_code == 422

    unsupported = _request(app, "POST", "/api/v1/operations", json={"op": "not-a-real-op"})
    assert unsupported.status_code == 422
    assert unsupported.json()["code"] == "unsupported_op"

    extra_field = _request(app, "POST", "/api/v1/operations", json={"op": "drift", "params": {"bogus": 1}})
    assert extra_field.status_code == 422
    assert extra_field.json()["code"] == "validation_error"


def test_create_operation_runs_to_completion_and_is_visible_via_get(tmp_path, monkeypatch):
    from nctl_core.drift_render import DriftData
    from nctl_core.serve import runner as runner_module

    monkeypatch.setattr(runner_module, "build_drift", lambda cfg, **kw: Envelope.build("nctl.drift.v1", DriftData()))

    app = create_app(_config(tmp_path))
    created = _request(app, "POST", "/api/v1/operations", json={"op": "drift", "params": {"host": "agpc"}})
    assert created.status_code == 202
    body = created.json()
    assert body["op"] == "drift"
    assert body["mutating"] is False
    operation_id = body["operation_id"]

    detail = _poll_finished(app, operation_id)
    operation = detail.json()["operation"]
    assert operation["ok"] is True
    assert detail.json()["result"]["schema"] == "nctl.drift.v1"

    events = _request(app, "GET", f"/api/v1/operations/{operation_id}/events")
    assert [e["event"] for e in events.json()["events"]] == ["started", "finished"]


def test_concurrent_mutating_post_returns_409_with_running_operation_id(tmp_path, monkeypatch):
    from nctl_core.events import OperationLog
    from nctl_core.reconcile.executor import ReconcileData
    from nctl_core.reconcile.model import PlanScope
    from nctl_core.serve import runner as runner_module

    release = threading.Event()
    started = threading.Event()

    def _slow_run_reconcile(cfg, *, host=None, apply_changes=False, max_rounds=None, operation_id=None, **_kw):
        log = OperationLog("reconcile", cfg.events.resolved_log_dir(), operation_id=operation_id)
        log.emit("started", "reconcile started")
        started.set()
        release.wait(timeout=2)
        data = ReconcileData(operation_id=operation_id, mode="apply", scope=PlanScope(kind="cluster"), event_log_path="")
        envelope = Envelope.build("nctl.reconcile.v2", data)
        log.finish(ok=True)
        return envelope

    monkeypatch.setattr(runner_module, "run_reconcile", _slow_run_reconcile)

    app = create_app(_config(tmp_path))
    first = _request(app, "POST", "/api/v1/operations", json={"op": "reconcile", "params": {"yes": True}})
    assert first.status_code == 202
    assert started.wait(timeout=2)

    second = _request(app, "POST", "/api/v1/operations", json={"op": "reconcile", "params": {"yes": True}})
    assert second.status_code == 409
    assert second.json()["detail"] == {"running_operation_id": first.json()["operation_id"]}

    release.set()
    _poll_finished(app, first.json()["operation_id"])
