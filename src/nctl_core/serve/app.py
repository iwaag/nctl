"""FastAPI application factory for the read-only Phase 5 server skeleton."""

from __future__ import annotations

import secrets
from importlib.metadata import PackageNotFoundError, version
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.openapi.utils import get_openapi
from starlette.exceptions import HTTPException as StarletteHTTPException

from nctl_core.config import Config, ConfigInvalidError
from nctl_core.operations_index import OperationIndexError, OperationRecord, list_operations, load_operation, read_events
from nctl_core.output import EnvelopeError
from nctl_core.serve.artifacts import list_public_artifacts, resolve_public_artifact
from nctl_core.serve.snapshots import latest_snapshot, read_result
from nctl_core.status import build_status


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str, detail: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self.error = EnvelopeError(code=code, message=message, detail=detail or {})


def create_app(cfg: Config) -> FastAPI:
    token = _resolved_serve_token(cfg)
    app = FastAPI(title="nctl subscriber API", version=_package_version(), openapi_url=None)
    app.state.nctl_config = cfg

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

    return app


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
