"""Read persisted command envelopes without triggering fresh computation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nctl_core.config import Config
from nctl_core.operations_index import list_operations


@dataclass(frozen=True)
class PersistedSnapshot:
    payload: dict[str, Any]
    path: Path
    operation_id: str | None = None


def latest_snapshot(cfg: Config, schema: str) -> PersistedSnapshot | None:
    """Return the newest persisted envelope matching ``schema``.

    Step 3's runner will persist terminal envelopes as ``result.json``. The
    dashboard's existing ``drift.json`` is also a valid Phase 3 snapshot and
    remains a useful fallback before the runner exists.
    """

    candidates: list[PersistedSnapshot] = []
    log_dir = cfg.events.resolved_log_dir()
    for operation in list_operations(log_dir):
        if operation.artifact_dir is None:
            continue
        path = Path(operation.artifact_dir) / "result.json"
        payload = _read_envelope(path, schema)
        if payload is not None:
            candidates.append(PersistedSnapshot(payload, path, operation.operation_id))

    if schema == "nctl.drift.v1":
        path = cfg.dashboard.resolved_out_dir() / "drift.json"
        payload = _read_envelope(path, schema)
        if payload is not None:
            candidates.append(PersistedSnapshot(payload, path))

    if not candidates:
        return None
    existing = [(item, _mtime(item.path)) for item in candidates]
    existing = [(item, mtime) for item, mtime in existing if mtime is not None]
    return max(existing, key=lambda pair: pair[1])[0] if existing else None


def read_result(artifact_dir: str | None) -> dict[str, Any] | None:
    if artifact_dir is None:
        return None
    path = Path(artifact_dir) / "result.json"
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _read_envelope(path: Path, schema: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != schema:
        return None
    if not isinstance(payload.get("ok"), bool) or not payload["ok"]:
        return None
    return payload


def _mtime(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None
