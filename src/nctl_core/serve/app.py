"""FastAPI application factory for the read-only Phase 5 server skeleton."""

from __future__ import annotations

import asyncio
import secrets
from importlib.metadata import PackageNotFoundError, version
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.openapi.utils import get_openapi
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.websockets import WebSocketState

from nctl_core.config import Config, ConfigInvalidError
from nctl_core.events import EventRecord, subscribe
from nctl_core.operations_index import OperationIndexError, OperationRecord, list_operations, load_operation, read_events
from nctl_core.output import EnvelopeError
from nctl_core.serve.artifacts import list_public_artifacts, resolve_public_artifact
from nctl_core.serve.runner import OperationRunner, RunnerError
from nctl_core.serve.snapshots import latest_snapshot, read_result
from nctl_core.status import build_status

_RUNNER_ERROR_STATUS = {"operation_conflict": 409}

# Close codes are in the 4000-4999 application-defined range (RFC 6455 7.4.2).
_WS_UNAUTHORIZED = 4401
_WS_BAD_SUBSCRIBE = 4400
_WS_SLOW_CONSUMER = 4408
_WS_SUBSCRIBE_TIMEOUT = 30.0
_WS_QUEUE_SIZE = 256


class _InvalidSubscribe(Exception):
    pass


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str, detail: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self.error = EnvelopeError(code=code, message=message, detail=detail or {})


def create_app(cfg: Config) -> FastAPI:
    token = _resolved_serve_token(cfg)
    app = FastAPI(title="nctl subscriber API", version=_package_version(), openapi_url=None)
    app.state.nctl_config = cfg
    runner = OperationRunner(cfg)
    app.state.nctl_runner = runner

    @app.exception_handler(ApiError)
    async def api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.error.model_dump(mode="json"))

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        error = EnvelopeError(
            code="validation_error",
            message="request validation failed",
            detail={"errors": exc.errors()},
        )
        return JSONResponse(status_code=422, content=error.model_dump(mode="json"))

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        error = EnvelopeError(code="not_found" if exc.status_code == 404 else "http_error", message=str(exc.detail))
        return JSONResponse(status_code=exc.status_code, content=error.model_dump(mode="json"))

    if cfg.serve.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cfg.serve.cors_origins,
            allow_credentials=False,
            allow_methods=["GET"],
            allow_headers=["Authorization"],
        )

    async def authorize(authorization: Annotated[str | None, Header()] = None) -> None:
        if cfg.serve.auth == "none":
            return
        scheme, separator, supplied = (authorization or "").partition(" ")
        if separator != " " or scheme.lower() != "bearer" or not supplied or token is None:
            raise ApiError(401, "unauthorized", "a valid bearer token is required")
        if not secrets.compare_digest(supplied, token):
            raise ApiError(401, "unauthorized", "a valid bearer token is required")

    @app.get("/api/v1/health", tags=["system"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": _package_version()}

    @app.get("/openapi.json", tags=["system"])
    async def openapi(_auth: None = Depends(authorize)) -> JSONResponse:
        if app.openapi_schema is None:
            app.openapi_schema = get_openapi(
                title=app.title,
                version=app.version,
                routes=app.routes,
            )
        return JSONResponse(content=app.openapi_schema)

    @app.get("/api/v1/status", tags=["snapshots"])
    async def status(refresh: bool = False, _auth: None = Depends(authorize)) -> Response:
        if refresh:
            return JSONResponse(content=build_status(cfg).model_dump(mode="json", by_alias=True))
        snapshot = latest_snapshot(cfg, "nctl.status.v1")
        if snapshot is None:
            raise ApiError(503, "snapshot_not_ready", "no persisted status snapshot is available")
        return _snapshot_response(snapshot.payload, snapshot.operation_id)

    @app.get("/api/v1/drift", tags=["snapshots"])
    async def drift(_auth: None = Depends(authorize)) -> Response:
        snapshot = latest_snapshot(cfg, "nctl.drift.v1")
        if snapshot is None:
            raise ApiError(503, "snapshot_not_ready", "no persisted drift snapshot is available")
        return _snapshot_response(snapshot.payload, snapshot.operation_id)

    @app.get("/api/v1/operations", tags=["operations"])
    async def operations(
        limit: Annotated[int | None, Query(ge=1, le=1000)] = None,
        _auth: None = Depends(authorize),
    ) -> dict[str, Any]:
        records = list_operations(cfg.events.resolved_log_dir(), limit=limit)
        return {"operations": [_public_operation(record, summary=True) for record in records]}

    @app.get("/api/v1/operations/{operation_id}", tags=["operations"])
    async def operation(operation_id: str, _auth: None = Depends(authorize)) -> dict[str, Any]:
        record = _operation_or_404(cfg, operation_id)
        return {"operation": _public_operation(record), "result": read_result(record.artifact_dir)}

    @app.get("/api/v1/operations/{operation_id}/events", tags=["operations"])
    async def operation_events(
        operation_id: str, after_seq: int = -1, _auth: None = Depends(authorize)
    ) -> dict[str, Any]:
        _operation_or_404(cfg, operation_id)
        records, corrupt = read_events(cfg.events.resolved_log_dir(), operation_id, after_seq=after_seq)
        return {
            "operation_id": operation_id,
            "events": [record.model_dump(mode="json") for record in records],
            "corrupt_lines": corrupt,
        }

    @app.get("/api/v1/operations/{operation_id}/artifacts", tags=["artifacts"])
    async def artifacts(operation_id: str, _auth: None = Depends(authorize)) -> dict[str, Any]:
        record = _operation_or_404(cfg, operation_id)
        return {
            "operation_id": operation_id,
            "artifacts": [artifact.model_dump(mode="json") for artifact in list_public_artifacts(record)],
        }

    @app.get("/api/v1/operations/{operation_id}/artifacts/{name:path}", tags=["artifacts"])
    async def artifact(operation_id: str, name: str, _auth: None = Depends(authorize)) -> Response:
        record = _operation_or_404(cfg, operation_id)
        path = resolve_public_artifact(record, name)
        if path is None:
            raise ApiError(404, "unknown_artifact", f"artifact is not publicly available: {name}")
        try:
            content = path.read_bytes()
        except OSError:
            raise ApiError(404, "unknown_artifact", f"artifact is not publicly available: {name}")
        return Response(content=content, media_type="application/json")

    @app.post("/api/v1/operations", tags=["operations"])
    async def create_operation(request: Request, _auth: None = Depends(authorize)) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            raise ApiError(422, "validation_error", "request body must be JSON")
        if not isinstance(body, dict) or not isinstance(body.get("op"), str):
            raise ApiError(422, "validation_error", 'request body must include a string "op" field')
        params = body.get("params", {})
        if not isinstance(params, dict):
            raise ApiError(422, "validation_error", '"params" must be an object')
        try:
            handle = runner.submit(body["op"], params)
        except RunnerError as exc:
            raise ApiError(_RUNNER_ERROR_STATUS.get(exc.code, 422), exc.code, exc.message, exc.detail)
        return JSONResponse(
            status_code=202,
            content={
                "operation_id": handle.operation_id,
                "op": handle.op,
                "mutating": handle.mutating,
                "events_url": f"/api/v1/operations/{handle.operation_id}/events",
                "ws_url": "/api/v1/ws",
            },
        )

    @app.websocket("/api/v1/ws")
    async def ws_stream(websocket: WebSocket) -> None:
        if not _ws_authorized(websocket, cfg, token):
            await websocket.close(code=_WS_UNAUTHORIZED)
            return
        await websocket.accept()
        try:
            raw = await asyncio.wait_for(websocket.receive_json(), timeout=_WS_SUBSCRIBE_TIMEOUT)
            operation_id, after_seq = _parse_subscribe(raw)
        except (TimeoutError, WebSocketDisconnect, ValueError, _InvalidSubscribe):
            await _safe_close(websocket, _WS_BAD_SUBSCRIBE)
            return

        loop = asyncio.get_running_loop()
        # One extra slot beyond _WS_QUEUE_SIZE is reserved for the `_OVERFLOW` sentinel, so it
        # can always be enqueued the moment the queue would otherwise be full.
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=_WS_QUEUE_SIZE + 1)
        overflowed = False

        def on_event(record: EventRecord) -> None:
            if operation_id is not None and record.operation_id != operation_id:
                return
            loop.call_soon_threadsafe(_offer, queue, record)

        unsubscribe = subscribe(on_event)
        try:
            seen: set[tuple[str, int]] = set()
            if operation_id is not None:
                records, _corrupt = read_events(cfg.events.resolved_log_dir(), operation_id, after_seq=after_seq)
                for record in records:
                    seen.add((record.operation_id, record.seq))
                    await websocket.send_json(record.model_dump(mode="json"))

            reader = asyncio.ensure_future(_drain_incoming(websocket))
            writer = asyncio.ensure_future(_write_events(websocket, queue, seen))
            done, pending = await asyncio.wait({reader, writer}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                exc = task.exception()
                if exc is not None and not isinstance(exc, WebSocketDisconnect):
                    raise exc
                if task is writer and exc is None and task.result():
                    overflowed = True
            if overflowed:
                await _safe_close(
                    websocket, _WS_SLOW_CONSUMER, "slow consumer; reconnect and replay via after_seq"
                )
        except WebSocketDisconnect:
            pass
        finally:
            # unsubscribe() joins the subscriber's worker thread; run off the event loop so a
            # slow-to-stop subscriber can't stall other connections.
            await asyncio.to_thread(unsubscribe)

    return app


_OVERFLOW = object()


def _offer(queue: "asyncio.Queue[Any]", record: EventRecord) -> None:
    """Runs on the event loop thread only (via `call_soon_threadsafe`), so the check-then-act
    below is not racy despite `subscribe()`'s callback originating on another OS thread."""

    if queue.full():
        return  # the `_OVERFLOW` sentinel (or another drop) already occupies the last slot
    if queue.qsize() >= _WS_QUEUE_SIZE:
        queue.put_nowait(_OVERFLOW)
        return
    queue.put_nowait(record)


async def _drain_incoming(websocket: WebSocket) -> None:
    """Detect client disconnect while the writer side is only sending."""

    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(message.get("code", 1000))


async def _write_events(websocket: WebSocket, queue: "asyncio.Queue[Any]", seen: set[tuple[str, int]]) -> bool:
    """Drains `queue` and sends each record; returns True if stopped by the overflow sentinel."""

    while True:
        item = await queue.get()
        if item is _OVERFLOW:
            return True
        record: EventRecord = item
        key = (record.operation_id, record.seq)
        if key in seen:
            continue
        seen.add(key)
        await websocket.send_json(record.model_dump(mode="json"))


async def _safe_close(websocket: WebSocket, code: int, reason: str = "") -> None:
    if websocket.application_state != WebSocketState.DISCONNECTED:
        await websocket.close(code=code, reason=reason)


def _parse_subscribe(raw: Any) -> tuple[str | None, int]:
    if not isinstance(raw, dict):
        raise _InvalidSubscribe("subscribe message must be a JSON object")
    after_seq = raw.get("after_seq", -1)
    if not isinstance(after_seq, int) or isinstance(after_seq, bool):
        raise _InvalidSubscribe('"after_seq" must be an integer')
    subscribe_target = raw.get("subscribe")
    if subscribe_target == "all":
        return None, after_seq
    if isinstance(subscribe_target, dict) and isinstance(subscribe_target.get("operation_id"), str):
        operation_id = subscribe_target["operation_id"]
        if operation_id:
            return operation_id, after_seq
    raise _InvalidSubscribe('"subscribe" must be "all" or {"operation_id": "..."}')


def _ws_authorized(websocket: WebSocket, cfg: Config, token: str | None) -> bool:
    if cfg.serve.auth == "none":
        return True
    if token is None:
        return False
    header = websocket.headers.get("authorization")
    scheme, separator, supplied = (header or "").partition(" ")
    if separator == " " and scheme.lower() == "bearer" and supplied and secrets.compare_digest(supplied, token):
        return True
    query_token = websocket.query_params.get("token")
    if query_token and secrets.compare_digest(query_token, token):
        return True
    return False


def _operation_or_404(cfg: Config, operation_id: str) -> OperationRecord:
    try:
        record = load_operation(cfg.events.resolved_log_dir(), operation_id)
    except OperationIndexError:
        raise ApiError(404, "unknown_operation", f"operation not found: {operation_id}")
    if record is None:
        raise ApiError(404, "unknown_operation", f"operation not found: {operation_id}")
    return record


def _public_operation(record: OperationRecord, *, summary: bool = False) -> dict[str, Any]:
    fields = {
        "operation_id",
        "op",
        "state",
        "ok",
        "result",
        "started_at",
        "updated_at",
        "last_seq",
        "event_count",
        "corrupt_lines",
    }
    payload = record.model_dump(mode="json", include=fields)
    if not summary:
        payload["artifacts"] = [artifact.model_dump(mode="json") for artifact in list_public_artifacts(record)]
    return payload


def _resolved_serve_token(cfg: Config) -> str | None:
    token = cfg.serve.resolve_token()
    if cfg.serve.auth == "token" and token is None:
        raise ConfigInvalidError(
            f"serve auth is enabled but no token was found in ${cfg.serve.token_env} or serve.token_file"
        )
    return token


def _snapshot_response(payload: dict[str, Any], operation_id: str | None) -> JSONResponse:
    headers = {"X-Nctl-Operation-Id": operation_id} if operation_id is not None else {}
    return JSONResponse(content=payload, headers=headers)


def _package_version() -> str:
    try:
        return version("nctl")
    except PackageNotFoundError:
        return "0.0.0"
