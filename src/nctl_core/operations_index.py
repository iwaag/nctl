"""Filesystem index over `[events].log_dir`: past and running operations, their events,
and their artifact files.

Pure reads — no Nautobot, no Ansible, no writes. The JSONL event file is the source of
truth for an operation's identity and state; the `<log_dir>/<operation_id>/` directory
(when present) holds its artifacts (`plan.json`, per-round drift, job records, ...).
Corrupted or partial JSONL lines are counted and skipped, never fatal: a crash mid-write
must not make the whole history unreadable.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ValidationError

from nctl_core.events import EventRecord

OPERATION_ID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


class OperationIndexError(RuntimeError):
    """An operation ID is malformed (and would escape the log directory as a path)."""


class OperationArtifact(BaseModel):
    name: str  # POSIX-style path relative to the operation's artifact directory
    size_bytes: int


class OperationRecord(BaseModel):
    operation_id: str
    op: str | None  # None when no event line could be parsed
    state: str  # "running" | "finished" | "no_events"
    ok: bool | None  # from the `finished` record's data.ok; None while running
    result: str | None  # the `finished` record's message (e.g. reconcile's terminal state)
    started_at: datetime | None
    updated_at: datetime | None
    last_seq: int | None
    event_count: int
    corrupt_lines: int
    log_path: str | None
    artifact_dir: str | None
    artifacts: list[OperationArtifact] = []


def validate_operation_id(operation_id: str) -> str:
    if not OPERATION_ID_RE.match(operation_id):
        raise OperationIndexError(f"malformed operation id: {operation_id!r}")
    return operation_id


def read_events(log_dir: Path, operation_id: str, after_seq: int = -1) -> tuple[list[EventRecord], int]:
    """Parse an operation's JSONL file; returns (records with seq > after_seq, corrupt line count)."""

    validate_operation_id(operation_id)
    path = log_dir / f"{operation_id}.jsonl"
    if not path.is_file():
        return [], 0
    records: list[EventRecord] = []
    corrupt = 0
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return [], 0
    for line in lines:
        if not line.strip():
            continue
        try:
            record = EventRecord.model_validate(json.loads(line))
        except (json.JSONDecodeError, ValidationError):
            corrupt += 1
            continue
        if record.seq > after_seq:
            records.append(record)
    return records, corrupt


def _list_artifacts(artifact_dir: Path) -> list[OperationArtifact]:
    artifacts: list[OperationArtifact] = []
    for path in sorted(artifact_dir.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        artifacts.append(OperationArtifact(name=path.relative_to(artifact_dir).as_posix(), size_bytes=size))
    return artifacts


def load_operation(log_dir: Path, operation_id: str) -> OperationRecord | None:
    """Build one operation's record from its JSONL file and artifact directory, or None if neither exists."""

    validate_operation_id(operation_id)
    log_path = log_dir / f"{operation_id}.jsonl"
    artifact_dir = log_dir / operation_id
    has_log = log_path.is_file()
    has_artifacts = artifact_dir.is_dir()
    if not has_log and not has_artifacts:
        return None

    events, corrupt = read_events(log_dir, operation_id)
    first = events[0] if events else None
    last = events[-1] if events else None
    if last is None:
        state = "no_events"
    elif last.event == "finished":
        state = "finished"
    else:
        state = "running"
    ok = last.data.get("ok") if state == "finished" and last is not None else None
    return OperationRecord(
        operation_id=operation_id,
        op=first.op if first is not None else None,
        state=state,
        ok=ok if isinstance(ok, bool) else None,
        result=last.message if state == "finished" and last is not None else None,
        started_at=first.ts if first is not None else None,
        updated_at=last.ts if last is not None else None,
        last_seq=last.seq if last is not None else None,
        event_count=len(events),
        corrupt_lines=corrupt,
        log_path=str(log_path) if has_log else None,
        artifact_dir=str(artifact_dir) if has_artifacts else None,
        artifacts=_list_artifacts(artifact_dir) if has_artifacts else [],
    )


def list_operations(log_dir: Path, limit: int | None = None) -> list[OperationRecord]:
    """Enumerate operations under `log_dir`, newest first (ULIDs sort by creation time)."""

    if not log_dir.is_dir():
        return []
    ids: set[str] = set()
    for entry in log_dir.iterdir():
        if entry.is_file() and entry.suffix == ".jsonl" and OPERATION_ID_RE.match(entry.stem):
            ids.add(entry.stem)
        elif entry.is_dir() and OPERATION_ID_RE.match(entry.name):
            ids.add(entry.name)
    records = []
    for operation_id in sorted(ids, reverse=True):
        if limit is not None and len(records) >= limit:
            break
        record = load_operation(log_dir, operation_id)
        if record is not None:
            records.append(record)
    return records
