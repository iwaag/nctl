"""Pure library logic for `nctl ops list` / `nctl ops show`: a thin CLI view over
`nctl_core.operations_index`.

These are snapshot reads (Phase 5 Decision 3): they never fetch from Nautobot, never
run Ansible, and — unlike real operations — never emit an event log of their own, so
inspecting history doesn't grow the history being inspected.
"""

from __future__ import annotations

from pydantic import BaseModel

from nctl_core.config import Config
from nctl_core.events import EventRecord
from nctl_core.operations_index import (
    OperationIndexError,
    OperationRecord,
    list_operations,
    load_operation,
    read_events,
)
from nctl_core.output import Envelope, EnvelopeError

OPS_LIST_SCHEMA = "nctl.ops.list.v1"
OPS_SHOW_SCHEMA = "nctl.ops.show.v1"


class OpsListData(BaseModel):
    log_dir: str
    operations: list[OperationRecord]


class OpsShowData(BaseModel):
    log_dir: str
    operation: OperationRecord | None = None
    events: list[EventRecord] = []


def build_ops_list(cfg: Config, limit: int | None = None) -> Envelope[OpsListData]:
    log_dir = cfg.events.resolved_log_dir()
    data = OpsListData(log_dir=str(log_dir), operations=list_operations(log_dir, limit=limit))
    return Envelope.build(OPS_LIST_SCHEMA, data)


def build_ops_show(cfg: Config, operation_id: str, after_seq: int = -1) -> Envelope[OpsShowData]:
    log_dir = cfg.events.resolved_log_dir()
    data = OpsShowData(log_dir=str(log_dir))
    errors: list[EnvelopeError] = []
    try:
        operation = load_operation(log_dir, operation_id)
    except OperationIndexError as exc:
        errors.append(EnvelopeError(code="malformed_operation_id", message=str(exc)))
        return Envelope.build(OPS_SHOW_SCHEMA, data, errors)
    if operation is None:
        errors.append(
            EnvelopeError(
                code="unknown_operation",
                message=f"no event log or artifacts for operation {operation_id}",
                detail={"operation_id": operation_id},
            )
        )
        return Envelope.build(OPS_SHOW_SCHEMA, data, errors)
    data.operation = operation
    data.events, _ = read_events(log_dir, operation_id, after_seq=after_seq)
    return Envelope.build(OPS_SHOW_SCHEMA, data, errors)


def render_ops_list_text(envelope: Envelope[OpsListData]) -> str:
    data = envelope.data
    lines = [f"log_dir: {data.log_dir}", f"operations: {len(data.operations)}"]
    for record in data.operations:
        started = record.started_at.isoformat() if record.started_at else "-"
        ok = "-" if record.ok is None else ("ok" if record.ok else "FAILED")
        result = f" {record.result}" if record.result else ""
        lines.append(
            f"  {record.operation_id}  {record.op or '?':<12} {record.state:<9} {ok:<7} {started}{result}"
        )
    for error in envelope.errors:
        lines.append(f"error[{error.code}]: {error.message}")
    return "\n".join(lines)


def render_ops_show_text(envelope: Envelope[OpsShowData]) -> str:
    lines: list[str] = []
    record = envelope.data.operation
    if record is not None:
        lines += [
            f"operation_id: {record.operation_id}",
            f"op: {record.op or '?'}",
            f"state: {record.state}" + (f" ({record.result})" if record.result else ""),
            f"ok: {'-' if record.ok is None else record.ok}",
            f"started_at: {record.started_at.isoformat() if record.started_at else '-'}",
            f"updated_at: {record.updated_at.isoformat() if record.updated_at else '-'}",
            f"events: {record.event_count} (last_seq={record.last_seq})",
        ]
        if record.corrupt_lines:
            lines.append(f"corrupt_lines: {record.corrupt_lines}")
        lines.append(f"event_log: {record.log_path or '-'}")
        if record.artifact_dir:
            lines.append(f"artifact_dir: {record.artifact_dir}")
            for artifact in record.artifacts:
                lines.append(f"    {artifact.name} ({artifact.size_bytes} bytes)")
        if envelope.data.events:
            lines.append("event tail:")
            for event in envelope.data.events:
                lines.append(f"  [{event.seq:>3}] {event.ts.isoformat()} {event.level:<7} {event.event}: {event.message}")
    for error in envelope.errors:
        lines.append(f"error[{error.code}]: {error.message}")
    return "\n".join(lines)
