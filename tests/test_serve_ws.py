"""WebSocket streaming tests for `/api/v1/ws` (Phase 5 Step 4).

Uses starlette's `TestClient.websocket_connect` (in-process ASGI, no real
socket) because `httpx.ASGITransport`, used by the rest of the serve test
suite, does not support the WebSocket protocol.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient, WebSocketDisconnect

from nctl_core.config import Config
from nctl_core.events import OperationLog
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


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_ws_requires_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("NCTL_SERVE_TOKEN_WS_AUTH", "s3cr3t")
    cfg = _config(tmp_path, serve={"auth": "token", "token_env": "NCTL_SERVE_TOKEN_WS_AUTH"})
    app = create_app(cfg)
    client = TestClient(app)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/api/v1/ws"):
            pass
    assert exc_info.value.code == 4401

    # a valid header works
    with client.websocket_connect("/api/v1/ws", headers={"Authorization": "Bearer s3cr3t"}) as ws:
        ws.send_json({"subscribe": "all", "after_seq": -1})

    # the query-param fallback works too
    with client.websocket_connect("/api/v1/ws?token=s3cr3t") as ws:
        ws.send_json({"subscribe": "all", "after_seq": -1})


def test_ws_replays_then_streams_live_for_one_operation(tmp_path):
    cfg = _config(tmp_path)
    app = create_app(cfg)
    client = TestClient(app)

    log = OperationLog.start("drift", cfg.events.resolved_log_dir())
    log.emit("step_started", "step one")  # seq 1, written before the client connects

    with client.websocket_connect("/api/v1/ws") as ws:
        ws.send_json({"subscribe": {"operation_id": log.operation_id}, "after_seq": -1})
        replayed = [ws.receive_json(), ws.receive_json()]
        assert [r["seq"] for r in replayed] == [0, 1]

        log.emit("step_completed", "step one done")  # seq 2, emitted live
        live = ws.receive_json()
        assert live["seq"] == 2
        assert live["event"] == "step_completed"

        log.finish(ok=True)  # seq 3
        finished = ws.receive_json()
        assert finished["event"] == "finished"


def test_ws_reconnect_replays_from_after_seq_without_gap_or_dup(tmp_path):
    cfg = _config(tmp_path)
    app = create_app(cfg)
    client = TestClient(app)

    log = OperationLog.start("drift", cfg.events.resolved_log_dir())
    log.emit("step_started", "a")
    log.emit("step_completed", "b")

    with client.websocket_connect("/api/v1/ws") as ws:
        ws.send_json({"subscribe": {"operation_id": log.operation_id}, "after_seq": 1})
        first = ws.receive_json()
        assert first["seq"] == 2

    # simulate disconnect/reconnect: the client says it has up to seq 2
    log.emit("step_completed", "c")  # seq 3, emitted while nobody is connected
    with client.websocket_connect("/api/v1/ws") as ws:
        ws.send_json({"subscribe": {"operation_id": log.operation_id}, "after_seq": 2})
        replay = ws.receive_json()
        assert replay["seq"] == 3
        log.finish(ok=True)  # seq 4, live
        live = ws.receive_json()
        assert live["seq"] == 4


def test_ws_subscribe_one_operation_ignores_other_operations(tmp_path):
    cfg = _config(tmp_path)
    app = create_app(cfg)
    client = TestClient(app)

    log_a = OperationLog.start("drift", cfg.events.resolved_log_dir())
    log_b = OperationLog.start("drift", cfg.events.resolved_log_dir())

    with client.websocket_connect("/api/v1/ws") as ws:
        ws.send_json({"subscribe": {"operation_id": log_a.operation_id}, "after_seq": -1})
        first = ws.receive_json()
        assert first["operation_id"] == log_a.operation_id

        log_b.emit("step_started", "irrelevant to this subscriber")
        log_a.emit("step_started", "relevant")
        live = ws.receive_json()
        assert live["operation_id"] == log_a.operation_id
        assert live["event"] == "step_started"


def test_ws_subscribe_all_fans_out_across_operations(tmp_path):
    cfg = _config(tmp_path)
    app = create_app(cfg)
    client = TestClient(app)

    with client.websocket_connect("/api/v1/ws") as ws:
        ws.send_json({"subscribe": "all", "after_seq": -1})

        log_a = OperationLog.start("drift", cfg.events.resolved_log_dir())
        log_b = OperationLog.start("dashboard", cfg.events.resolved_log_dir())

        seen_ops = {ws.receive_json()["operation_id"], ws.receive_json()["operation_id"]}
        assert seen_ops == {log_a.operation_id, log_b.operation_id}


def test_ws_multi_client_fanout(tmp_path):
    cfg = _config(tmp_path)
    app = create_app(cfg)
    client = TestClient(app)

    log = OperationLog.start("drift", cfg.events.resolved_log_dir())

    with client.websocket_connect("/api/v1/ws") as ws1, client.websocket_connect("/api/v1/ws") as ws2:
        ws1.send_json({"subscribe": {"operation_id": log.operation_id}, "after_seq": -1})
        ws2.send_json({"subscribe": {"operation_id": log.operation_id}, "after_seq": -1})
        assert ws1.receive_json()["seq"] == 0
        assert ws2.receive_json()["seq"] == 0

        log.emit("step_started", "fan out")
        assert ws1.receive_json()["seq"] == 1
        assert ws2.receive_json()["seq"] == 1


def test_ws_bad_subscribe_message_closes_with_4400(tmp_path):
    cfg = _config(tmp_path)
    app = create_app(cfg)
    client = TestClient(app)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/api/v1/ws") as ws:
            ws.send_json({"subscribe": "not-a-valid-target"})
            ws.receive_json()
    assert exc_info.value.code == 4400


def test_ws_slow_consumer_is_disconnected_not_buffered_unboundedly(tmp_path, monkeypatch):
    # TestClient's in-process ASGI transport buffers sends unboundedly (no real socket
    # backpressure), so a real client can always outpace any flood of events, no matter how
    # fast -- the writer simply keeps draining the queue as fast as it fills. To exercise the
    # overflow path deterministically (rather than racing against scheduling), shrink the
    # server's internal queue to zero: the very first live event is guaranteed to overflow it.
    monkeypatch.setattr(app_module, "_WS_QUEUE_SIZE", 0)
    cfg = _config(tmp_path)
    app = create_app(cfg)
    client = TestClient(app)

    log = OperationLog.start("drift", cfg.events.resolved_log_dir())

    with client.websocket_connect("/api/v1/ws") as ws:
        ws.send_json({"subscribe": {"operation_id": log.operation_id}, "after_seq": -1})
        ws.receive_json()  # the "started" replay record

        log.emit("step_completed", "this overflows the zero-sized queue")

        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_json()
        assert exc_info.value.code == 4408


def test_ws_client_disconnect_stops_the_bus_subscription(tmp_path):
    from nctl_core import events as events_module

    cfg = _config(tmp_path)
    app = create_app(cfg)
    client = TestClient(app)

    with client.websocket_connect("/api/v1/ws") as ws:
        ws.send_json({"subscribe": "all", "after_seq": -1})

    assert _wait_until(lambda: not events_module._subscribers)


def test_post_operations_response_includes_ws_url(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    app = create_app(cfg)
    client = TestClient(app)
    response = client.post("/api/v1/operations", json={"op": "drift", "params": {}})
    assert response.status_code == 202
    assert response.json()["ws_url"] == "/api/v1/ws"
