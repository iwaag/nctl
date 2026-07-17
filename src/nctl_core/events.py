"""JSON Lines event log for long-running operations. See docs/event-log.md."""

from __future__ import annotations

import os
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

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


Subscriber = Callable[[EventRecord], None]


class _SubscriberEntry:
    """One subscriber's bounded delivery queue and its dedicated worker thread.

    Delivery is FIFO in emit order. When the queue is full the oldest pending
    record is dropped (correctness comes from JSONL replay, not the bus), and
    the drop is warned about once on stderr. A raising callback is likewise
    warned about once and never propagates back into `emit`.
    """

    def __init__(self, callback: Subscriber, max_pending: int) -> None:
        self.callback = callback
        self._pending: deque[EventRecord] = deque()
        self._max_pending = max_pending
        self._condition = threading.Condition()
        self._stopped = False
        self.dropped = 0
        self._warned_drop = False
        self._warned_error = False
        self._thread = threading.Thread(target=self._run, daemon=True, name="nctl-event-subscriber")
        self._thread.start()

    def offer(self, record: EventRecord) -> None:
        with self._condition:
            if self._stopped:
                return
            if len(self._pending) >= self._max_pending:
                self._pending.popleft()
                self.dropped += 1
                if not self._warned_drop:
                    print(
                        f"warning: event subscriber queue full ({self._max_pending}); dropping oldest "
                        "pending events (JSONL file replay remains lossless)",
                        file=sys.stderr,
                    )
                    self._warned_drop = True
            self._pending.append(record)
            self._condition.notify()

    def stop(self) -> None:
        with self._condition:
            self._stopped = True
            self._condition.notify()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._pending and not self._stopped:
                    self._condition.wait()
                if self._stopped and not self._pending:
                    return
                record = self._pending.popleft()
            try:
                self.callback(record)
            except Exception as exc:  # noqa: BLE001 - subscriber isolation contract
                if not self._warned_error:
                    print(f"warning: event subscriber raised and was muted: {exc!r}", file=sys.stderr)
                    self._warned_error = True


_subscribers: list[_SubscriberEntry] = []
_subscribers_lock = threading.Lock()


def subscribe(callback: Subscriber, *, max_pending: int = 1024) -> Callable[[], None]:
    """Register a process-wide event subscriber; returns an idempotent unsubscribe callable.

    The callback runs on a dedicated worker thread, receives records in emit
    order, and is subject to the same never-crash-the-command contract as the
    file write: exceptions are reported once to stderr and swallowed, and a
    slow subscriber loses oldest-first from a bounded queue rather than
    blocking `emit`. The JSONL file stays the source of truth.
    """

    entry = _SubscriberEntry(callback, max_pending)
    with _subscribers_lock:
        _subscribers.append(entry)

    def unsubscribe() -> None:
        with _subscribers_lock:
            if entry in _subscribers:
                _subscribers.remove(entry)
        entry.stop()

    return unsubscribe


def _publish(record: EventRecord) -> None:
    with _subscribers_lock:
        entries = list(_subscribers)
    for entry in entries:
        entry.offer(record)


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
        else:
            _publish(record)
