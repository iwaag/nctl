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
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ValidationError

from nctl_core.events import EventRecord

OPERATION_ID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")

# F4 (no_guest_vm follow-up): a "running" operation whose last event is older
# than this is almost certainly a process that died without its `finished`
# event (kill -9, crash). It is *reported* as stale, never auto-closed --
# closing is an explicit `nctl ops close` decision with a recorded reason.
STALE_RUNNING_SECONDS = 3600


class OperationIndexError(RuntimeError):
    """An operation ID is malformed (and would escape the log directory as a path)."""


class OperationArtifact(BaseModel):
    name: str  # POSIX-style path relative to the operation's artifact directory
    size_bytes: int


class OperationRecord(BaseModel):
    operation_id: str
    op: str | None  # None when no event line could be parsed
    state: str  # "running" | "finished" | "no_events"
    stale: bool = False  # running, but silent past STALE_RUNNING_SECONDS (owner likely died)
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


def load_operation(log_dir: Path, operation_id: str, *, now: datetime | None = None) -> OperationRecord | None:
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
    reference = now or datetime.now(timezone.utc)
    stale = state == "running" and last is not None and (reference - last.ts).total_seconds() > STALE_RUNNING_SECONDS
    ok = last.data.get("ok") if state == "finished" and last is not None else None
    return OperationRecord(
        operation_id=operation_id,
        op=first.op if first is not None else None,
        state=state,
        stale=stale,
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


class OperationCloseError(RuntimeError):
    """The operation cannot be closed (unknown, already finished, or not stale)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def close_operation(
    log_dir: Path, operation_id: str, *, reason: str, force: bool = False, now: datetime | None = None
) -> OperationRecord:
    """Append an explicit abandonment `finished` event to a stale running operation.

    This is the one sanctioned write in this module: it closes an operation
    whose owning process died without its `finished` event, so history stops
    reporting it as running forever. The event log stays append-only -- nothing
    is rewritten -- and the closure records who/why (`abandoned: <reason>`).
    A recently-active operation is refused unless `force` is set, because it
    may genuinely still be running.
    """

    reference = now or datetime.now(timezone.utc)
    record = load_operation(log_dir, operation_id, now=reference)
    if record is None:
        raise OperationCloseError("unknown_operation", f"no event log or artifacts for operation {operation_id}")
    if record.state == "finished":
        raise OperationCloseError("already_finished", f"operation {operation_id} already has a finished event")
    if record.state == "no_events":
        raise OperationCloseError("no_events", f"operation {operation_id} has no parseable events to close")
    if not record.stale and not force:
        raise OperationCloseError(
            "not_stale",
            f"operation {operation_id} was active within the last {STALE_RUNNING_SECONDS}s; "
            "it may still be running -- rerun with --force to close it anyway",
        )
    event = EventRecord(
        ts=reference,
        operation_id=operation_id,
        op=record.op or "unknown",
        seq=(record.last_seq or 0) + 1,
        event="finished",
        level="warning",
        message=f"abandoned: {reason}",
        data={"ok": False, "abandoned": True, "closed_by": "nctl ops close"},
    )
    with (log_dir / f"{operation_id}.jsonl").open("a") as handle:
        handle.write(event.model_dump_json() + "\n")
    closed = load_operation(log_dir, operation_id, now=reference)
    assert closed is not None
    return closed
