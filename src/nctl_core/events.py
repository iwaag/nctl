"""JSON Lines event log for long-running operations. See docs/event-log.md."""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def generate_ulid() -> str:
    """A ULID: 48-bit millisecond timestamp + 80-bit randomness, Crockford base32, 26 chars."""
    ms = int(time.time() * 1000)
    value = (ms << 80) | int.from_bytes(os.urandom(10), "big")
    chars = []
    for _ in range(26):
        value, rem = divmod(value, 32)
        chars.append(_CROCKFORD_ALPHABET[rem])
    return "".join(reversed(chars))


class EventRecord(BaseModel):
    ts: datetime
    operation_id: str
    op: str
    seq: int
    event: str
    level: str = "info"
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class OperationLog:
    """Emits one JSONL file per operation at `<log_dir>/<operation_id>.jsonl`.

    A failure to write events must never crash the command it's instrumenting;
    write errors are reported to stderr once and then swallowed.
    """

    def __init__(self, op: str, log_dir: Path, operation_id: str | None = None) -> None:
        self.op = op
        self.operation_id = operation_id or generate_ulid()
        self.log_dir = log_dir
        self.path = log_dir / f"{self.operation_id}.jsonl"
        self._seq = 0
        self._warned = False

    @classmethod
    def start(cls, op: str, log_dir: Path) -> "OperationLog":
        log = cls(op, log_dir)
        log.emit("started", f"{op} started")
        return log

    def emit(self, event: str, message: str = "", level: str = "info", **data: Any) -> EventRecord:
        record = EventRecord(
            ts=datetime.now(timezone.utc),
            operation_id=self.operation_id,
            op=self.op,
            seq=self._seq,
            event=event,
            level=level,
            message=message,
            data=data,
        )
        self._seq += 1
        self._write(record)
        return record

    def finish(self, ok: bool, message: str = "") -> EventRecord:
        return self.emit("finished", message or ("ok" if ok else "failed"), ok=ok)

    def _write(self, record: EventRecord) -> None:
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as f:
                f.write(record.model_dump_json() + "\n")
        except OSError as exc:
            if not self._warned:
                print(f"warning: failed to write event log ({self.path}): {exc}", file=sys.stderr)
                self._warned = True
