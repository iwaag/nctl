import asyncio
import importlib
import json
import os
from pathlib import Path

import httpx
import pytest

from nctl_core.artifacts import OperationArtifacts
from nctl_core.config import Config, ConfigInvalidError, ServeConfig
from nctl_core.events import OperationLog
from nctl_core.serve.app import create_app


def _config(tmp_path: Path, *, auth="token", host="127.0.0.1") -> Config:
    return Config.model_validate(
        {
            "nautobot": {"url": "http://nautobot.test"},
            "inventory": {"dumps_dir": tmp_path / "dumps"},
            "events": {"log_dir": tmp_path / "events"},
            "ansible": {"playbook_dir": tmp_path / "ansible", "inventory": "inventory.yml"},
            "dashboard": {"out_dir": tmp_path / "dashboard"},
            "serve": {"auth": auth, "host": host},
            "source_path": tmp_path / "nctl.toml",
        }
    )


def _request(app, method, path, **kwargs):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(run())


def _auth():
    return {"Authorization": "Bearer test-serve-token"}


def _write_result(log_dir: Path, schema: str, *, ok=True):
    log = OperationLog.start("drift" if schema == "nctl.drift.v1" else "status", log_dir)
    artifacts = OperationArtifacts.create(log_dir, log.operation_id)
    payload = {
        "schema": schema,
        "generated_at": "2026-07-18T00:00:00Z",
        "ok": ok,
        "data": {"marker": log.operation_id},
        "errors": [],
    }
    path = artifacts.write_json("result.json", payload)
    os.chmod(path, 0o644)
    log.finish(ok=ok)
    return log, payload, artifacts


def test_health_is_the_only_endpoint_not_requiring_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("NCTL_SERVE_TOKEN", "test-serve-token")
    app = create_app(_config(tmp_path))

    health = _request(app, "GET", "/api/v1/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    missing = _request(app, "GET", "/api/v1/operations")
    wrong = _request(app, "GET", "/api/v1/operations", headers={"Authorization": "Bearer wrong"})
    assert missing.status_code == wrong.status_code == 401
    assert missing.json() == {
        "code": "unauthorized",
        "message": "a valid bearer token is required",
        "detail": {},
    }


def test_token_auth_fails_fast_when_token_is_unresolved(tmp_path, monkeypatch):
    monkeypatch.delenv("NCTL_SERVE_TOKEN", raising=False)
    with pytest.raises(ConfigInvalidError, match="no token"):
        create_app(_config(tmp_path))


def test_explicit_none_auth_works_on_loopback(tmp_path):
    app = create_app(_config(tmp_path, auth="none"))
    response = _request(app, "GET", "/api/v1/operations")
    assert response.status_code == 200
    assert response.json() == {"operations": []}

    with pytest.raises(ValueError, match="loopback"):
        ServeConfig(host="0.0.0.0", auth="none")


def test_snapshot_endpoints_return_persisted_envelopes_without_computation(tmp_path, monkeypatch):
    monkeypatch.setenv("NCTL_SERVE_TOKEN", "test-serve-token")
    cfg = _config(tmp_path)
    status_log, status_payload, _ = _write_result(cfg.events.resolved_log_dir(), "nctl.status.v1")
    drift_log, drift_payload, _ = _write_result(cfg.events.resolved_log_dir(), "nctl.drift.v1")
    app = create_app(cfg)

    status = _request(app, "GET", "/api/v1/status", headers=_auth())
    drift = _request(app, "GET", "/api/v1/drift", headers=_auth())
    assert status.status_code == drift.status_code == 200
    assert status.json() == status_payload
    assert drift.json() == drift_payload
    assert status.headers["x-nctl-operation-id"] == status_log.operation_id
    assert drift.headers["x-nctl-operation-id"] == drift_log.operation_id


def test_drift_falls_back_to_existing_dashboard_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("NCTL_SERVE_TOKEN", "test-serve-token")
    cfg = _config(tmp_path)
    cfg.dashboard.resolved_out_dir().mkdir()
    payload = {
        "schema": "nctl.drift.v1",
        "generated_at": "2026-07-18T00:00:00Z",
        "ok": True,
        "data": {},
        "errors": [],
    }
    (cfg.dashboard.resolved_out_dir() / "drift.json").write_text(json.dumps(payload))
    response = _request(create_app(cfg), "GET", "/api/v1/drift", headers=_auth())
    assert response.status_code == 200
    assert response.json() == payload
    assert "x-nctl-operation-id" not in response.headers


def test_missing_or_failed_snapshot_is_not_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("NCTL_SERVE_TOKEN", "test-serve-token")
    cfg = _config(tmp_path)
    _write_result(cfg.events.resolved_log_dir(), "nctl.drift.v1", ok=False)
    response = _request(create_app(cfg), "GET", "/api/v1/drift", headers=_auth())
    assert response.status_code == 503
    assert response.json()["code"] == "snapshot_not_ready"


def test_status_refresh_is_the_explicit_synchronous_exception(tmp_path, monkeypatch):
    monkeypatch.setenv("NCTL_SERVE_TOKEN", "test-serve-token")
    cfg = _config(tmp_path)
    payload = {
        "schema": "nctl.status.v1",
        "generated_at": "2026-07-18T00:00:00Z",
        "ok": True,
        "data": {"fresh": True},
        "errors": [],
    }

    class CannedEnvelope:
        def model_dump(self, **_kwargs):
            return payload

    serve_app = importlib.import_module("nctl_core.serve.app")
    monkeypatch.setattr(serve_app, "build_status", lambda value: CannedEnvelope())
    response = _request(create_app(cfg), "GET", "/api/v1/status?refresh=true", headers=_auth())
    assert response.status_code == 200
    assert response.json() == payload


def test_operations_detail_events_and_unknown_id(tmp_path, monkeypatch):
    monkeypatch.setenv("NCTL_SERVE_TOKEN", "test-serve-token")
    cfg = _config(tmp_path)
    log, payload, _ = _write_result(cfg.events.resolved_log_dir(), "nctl.drift.v1")
    app = create_app(cfg)

    listing = _request(app, "GET", "/api/v1/operations", headers=_auth())
    assert listing.status_code == 200
    assert listing.json()["operations"][0]["operation_id"] == log.operation_id
    assert "artifact_dir" not in listing.json()["operations"][0]
    assert "log_path" not in listing.json()["operations"][0]

    detail = _request(app, "GET", f"/api/v1/operations/{log.operation_id}", headers=_auth())
    assert detail.status_code == 200
    assert detail.json()["result"] == payload

    events = _request(app, "GET", f"/api/v1/operations/{log.operation_id}/events?after_seq=0", headers=_auth())
    assert events.status_code == 200
    assert [record["seq"] for record in events.json()["events"]] == [1]

    unknown = _request(app, "GET", "/api/v1/operations/not-an-id", headers=_auth())
    assert unknown.status_code == 404
    assert unknown.json()["code"] == "unknown_operation"

    invalid_limit = _request(app, "GET", "/api/v1/operations?limit=0", headers=_auth())
    assert invalid_limit.status_code == 422
    assert invalid_limit.json()["code"] == "validation_error"


def test_artifacts_are_allowlisted_confined_and_never_private_or_symlinked(tmp_path, monkeypatch):
    monkeypatch.setenv("NCTL_SERVE_TOKEN", "test-serve-token")
    cfg = _config(tmp_path)
    log, _, artifacts = _write_result(cfg.events.resolved_log_dir(), "nctl.drift.v1")
    public = artifacts.write_json("round-00/drift-final.json", {"safe": True})
    os.chmod(public, 0o644)
    private = artifacts.write_json("plan.json", {"private": True})
    assert private.stat().st_mode & 0o777 == 0o600
    report = artifacts.write_json("round-00/reports/node.json", {"secret": True})
    os.chmod(report, 0o644)
    outside = tmp_path / "outside.json"
    outside.write_text('{"outside": true}')
    symlink = artifacts.root / "drift.json"
    symlink.symlink_to(outside)
    app = create_app(cfg)
    base = f"/api/v1/operations/{log.operation_id}/artifacts"

    listing = _request(app, "GET", base, headers=_auth())
    assert listing.status_code == 200
    assert [item["name"] for item in listing.json()["artifacts"]] == [
        "result.json",
        "round-00/drift-final.json",
    ]

    fetched = _request(app, "GET", f"{base}/round-00/drift-final.json", headers=_auth())
    assert fetched.status_code == 200
    assert fetched.json() == {"safe": True}

    for name in ("plan.json", "round-00/reports/node.json", "drift.json", "../outside.json"):
        denied = _request(app, "GET", f"{base}/{name}", headers=_auth())
        assert denied.status_code == 404


def test_openapi_contains_step2_read_surface(tmp_path, monkeypatch):
    monkeypatch.setenv("NCTL_SERVE_TOKEN", "test-serve-token")
    app = create_app(_config(tmp_path))
    assert _request(app, "GET", "/openapi.json").status_code == 401
    document = _request(app, "GET", "/openapi.json", headers=_auth()).json()
    paths = set(document["paths"])
    assert {
        "/api/v1/health",
        "/api/v1/status",
        "/api/v1/drift",
        "/api/v1/operations",
        "/api/v1/operations/{operation_id}",
        "/api/v1/operations/{operation_id}/events",
        "/api/v1/operations/{operation_id}/artifacts",
        "/api/v1/operations/{operation_id}/artifacts/{name}",
    } <= paths
