"""Operation executor for `POST /api/v1/operations` (Phase 5 Step 3).

Every supported `op` maps one-to-one onto an existing `nctl_core` build
function -- this module never invents new business logic, only the plumbing
that lets those synchronous, potentially long-running calls run on a worker
thread, get a `202`-able `operation_id` immediately, and leave behind the
same JSONL/artifact layout a CLI invocation would.

`reconcile` already owns its `OperationLog`/`OperationArtifacts` lifecycle
(Phase 4); this module only threads a pre-generated `operation_id` into it so
the ID handed back in the `202` response matches the JSONL file the worker
thread goes on to write. `drift`/`dashboard`/`render.*` have no such
lifecycle of their own (they are deliberately side-effect-free, ID-less reads
under the CLI) -- `_wrapped` gives them one only for the duration of a
server-triggered run, so `nctl ops show`/`GET /operations/{id}` can see them
too.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from nctl_core.artifacts import ArtifactError, OperationArtifacts
from nctl_core.config import Config
from nctl_core.dashboard_render import build_dashboard
from nctl_core.dnsmasq_render import build_dnsmasq_render
from nctl_core.drift_render import build_drift
from nctl_core.events import OperationLog, generate_ulid
from nctl_core.hosts_intent_render import build_hosts_intent_render, write_hosts_intent_artifacts
from nctl_core.output import Envelope
from nctl_core.production_render import build_production_render, write_production_artifacts
from nctl_core.reconcile.executor import run_reconcile

SUPPORTED_OPS = ("drift", "dashboard", "render.dnsmasq", "render.production", "render.hosts_intent", "reconcile")


class RunnerError(Exception):
    """A submission cannot proceed; carries the shape `serve.app` turns into an HTTP error."""

    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


class _StrictParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DriftParams(_StrictParams):
    host: str | None = None
    service: str | None = None


class DashboardParams(_StrictParams):
    no_push: bool = False


class RenderDnsmasqParams(_StrictParams):
    pass


class RenderProductionParams(_StrictParams):
    # There is exactly one canonical destination (the configured
    # `ansible.inventory` directory); `write` selects it or leaves the run
    # compute-only, rather than accepting an arbitrary path over the network.
    write: bool = False


class RenderHostsIntentParams(_StrictParams):
    write: bool = False


class ReconcileParams(_StrictParams):
    host: str | None = None
    yes: bool = False
    max_rounds: int | None = Field(default=None, ge=1, le=10)


_PARAM_MODELS: dict[str, type[BaseModel]] = {
    "drift": DriftParams,
    "dashboard": DashboardParams,
    "render.dnsmasq": RenderDnsmasqParams,
    "render.production": RenderProductionParams,
    "render.hosts_intent": RenderHostsIntentParams,
    "reconcile": ReconcileParams,
}


def parse_params(op: str, raw: dict[str, Any]) -> BaseModel:
    model = _PARAM_MODELS.get(op)
    if model is None:
        raise RunnerError("unsupported_op", f"unsupported op: {op!r}", {"supported": list(SUPPORTED_OPS)})
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise RunnerError("validation_error", "invalid operation params", {"errors": exc.errors()}) from exc


def is_mutating(op: str, params: BaseModel) -> bool:
    """Mutating ops hold the gate's exclusive writer slot; everything else is a reader.

    `dashboard` always writes the configured out dir (and, unless `no_push`,
    pushes statuses to Nautobot) so it is always mutating. `reconcile` is
    mutating only with `yes=true` -- plan mode touches nothing shared.
    `render.production`/`render.hosts_intent` are mutating only when `write`
    targets the canonical inventory path; compute-only renders, and
    `render.dnsmasq` (which has no canonical destination over the API), are
    read-only.
    """

    if op == "dashboard":
        return True
    if op == "reconcile":
        assert isinstance(params, ReconcileParams)
        return params.yes
    if op in ("render.production", "render.hosts_intent"):
        assert isinstance(params, (RenderProductionParams, RenderHostsIntentParams))
        return params.write
    return False


@dataclass
class OperationHandle:
    operation_id: str
    op: str
    mutating: bool
    state: str = "accepted"  # accepted | running | finished
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    error: str | None = None


class _Gate:
    """A non-blocking readers-writer gate over the single-flight slot.

    Mutating ops are the writer: exclusive against every other op, mutating
    or not. Read-only ops are readers: any number may run together, but are
    excluded by an active writer (Phase 4's file-lock reasons -- inventory
    replacement, Job races -- apply to the server unchanged). There is
    deliberately no queue: an op that cannot start right now fails immediately
    with the ID of whatever is holding the gate, which is what a `409`
    response needs.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._writer: str | None = None
        self._readers: dict[str, None] = {}

    def enter(self, operation_id: str, mutating: bool) -> None:
        with self._lock:
            if mutating:
                blocker = self._writer or next(iter(self._readers), None)
                if blocker is not None:
                    raise RunnerError(
                        "operation_conflict",
                        "another operation is running and must finish first",
                        {"running_operation_id": blocker},
                    )
                self._writer = operation_id
            else:
                if self._writer is not None:
                    raise RunnerError(
                        "operation_conflict",
                        "a mutating operation is running",
                        {"running_operation_id": self._writer},
                    )
                self._readers[operation_id] = None

    def leave(self, operation_id: str, mutating: bool) -> None:
        with self._lock:
            if mutating:
                if self._writer == operation_id:
                    self._writer = None
            else:
                self._readers.pop(operation_id, None)


class OperationRunner:
    """Owns in-process operation state for one running server instance."""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._gate = _Gate()
        self._handles: dict[str, OperationHandle] = {}
        self._handles_lock = threading.Lock()

    def submit(self, op: str, raw_params: dict[str, Any]) -> OperationHandle:
        params = parse_params(op, raw_params)
        mutating = is_mutating(op, params)
        operation_id = generate_ulid()
        self._gate.enter(operation_id, mutating)
        handle = OperationHandle(operation_id=operation_id, op=op, mutating=mutating)
        with self._handles_lock:
            self._handles[operation_id] = handle
        threading.Thread(
            target=self._run, args=(handle, params), name=f"nctl-op-{operation_id}", daemon=True
        ).start()
        return handle

    def get(self, operation_id: str) -> OperationHandle | None:
        with self._handles_lock:
            return self._handles.get(operation_id)

    def _run(self, handle: OperationHandle, params: BaseModel) -> None:
        handle.state = "running"
        try:
            _execute(self._cfg, handle.operation_id, handle.op, params)
        except Exception as exc:  # noqa: BLE001 - a worker thread must never crash the server
            handle.error = repr(exc)
        finally:
            handle.state = "finished"
            self._gate.leave(handle.operation_id, handle.mutating)


def _execute(cfg: Config, operation_id: str, op: str, params: BaseModel) -> Envelope[Any]:
    if op == "reconcile":
        assert isinstance(params, ReconcileParams)
        return run_reconcile(
            cfg, host=params.host, apply_changes=params.yes, max_rounds=params.max_rounds, operation_id=operation_id
        )
    if op == "drift":
        assert isinstance(params, DriftParams)
        return _wrapped(cfg, operation_id, op, lambda: build_drift(cfg, host=params.host, service=params.service))
    if op == "dashboard":
        assert isinstance(params, DashboardParams)
        return _wrapped(cfg, operation_id, op, lambda: build_dashboard(cfg, push=not params.no_push))
    if op == "render.dnsmasq":
        return _wrapped(cfg, operation_id, op, lambda: build_dnsmasq_render(cfg, operation_id=operation_id))
    if op == "render.production":
        assert isinstance(params, RenderProductionParams)
        return _wrapped(cfg, operation_id, op, lambda: _render_production(cfg, params))
    if op == "render.hosts_intent":
        assert isinstance(params, RenderHostsIntentParams)
        return _wrapped(cfg, operation_id, op, lambda: _render_hosts_intent(cfg, params))
    raise RunnerError("unsupported_op", f"unsupported op: {op!r}", {"supported": list(SUPPORTED_OPS)})


def _render_production(cfg: Config, params: RenderProductionParams) -> Envelope[Any]:
    envelope = build_production_render(cfg)
    if params.write and envelope.ok:
        out_dir = cfg.ansible.resolved_inventory(cfg.source_path.parent).parent
        write_error = write_production_artifacts(envelope, out_dir)
        if write_error is not None:
            envelope = envelope.model_copy(update={"ok": False, "errors": [write_error]})
    return envelope


def _render_hosts_intent(cfg: Config, params: RenderHostsIntentParams) -> Envelope[Any]:
    envelope = build_hosts_intent_render(cfg)
    if params.write and envelope.ok:
        out_dir = cfg.ansible.resolved_inventory(cfg.source_path.parent).parent
        write_error = write_hosts_intent_artifacts(envelope, out_dir)
        if write_error is not None:
            envelope = envelope.model_copy(update={"ok": False, "errors": [write_error]})
    return envelope


def _wrapped(cfg: Config, operation_id: str, op_label: str, fn: Callable[[], Envelope[Any]]) -> Envelope[Any]:
    """Give a CLI-side, event-log-less build function a real operation lifecycle for this run."""

    log = OperationLog(op_label, cfg.events.resolved_log_dir(), operation_id=operation_id)
    log.emit("started", f"{op_label} started")
    envelope = fn()
    # Persist before emitting `finished`: a reader that observes the terminal event (via the
    # JSONL file, `nctl ops show`, or `GET /operations/{id}`) must always find `result.json`
    # already in place, never a window where the state is "finished" but the artifact isn't.
    _persist_result(cfg.events.resolved_log_dir(), operation_id, envelope)
    log.finish(ok=envelope.ok, message="ok" if envelope.ok else "failed")
    return envelope


def _persist_result(log_dir: Path, operation_id: str, envelope: Envelope[Any]) -> None:
    try:
        artifacts = OperationArtifacts.create(log_dir, operation_id)
        path = artifacts.write_json("result.json", envelope.model_dump(mode="json", by_alias=True))
        path.chmod(0o644)
    except ArtifactError:
        pass
