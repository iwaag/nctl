"""Subscriber-bus contract: emit order, isolation from raising/slow subscribers,
and the file remaining the source of truth."""

import json
import threading
import time

from nctl_core import events as events_module
from nctl_core.events import OperationLog, subscribe


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_subscriber_receives_records_in_emit_order_matching_file(tmp_path):
    received = []
    unsubscribe = subscribe(received.append)
    try:
        op = OperationLog.start("status", tmp_path / "events")
        for i in range(5):
            op.emit("step_completed", f"step {i}", index=i)
        op.finish(ok=True)
        assert _wait_until(lambda: len(received) == 7)
    finally:
        unsubscribe()

    file_records = [json.loads(line) for line in op.path.read_text().splitlines()]
    assert [r.seq for r in received] == [r["seq"] for r in file_records] == list(range(7))
    assert [r.event for r in received] == [r["event"] for r in file_records]


def test_raising_subscriber_is_muted_and_warns_once(tmp_path, capsys):
    calls = []

    def bad_subscriber(record):
        calls.append(record.seq)
        raise RuntimeError("boom")

    unsubscribe = subscribe(bad_subscriber)
    try:
        op = OperationLog.start("status", tmp_path / "events")
        op.emit("step_completed", "still fine")
        op.finish(ok=True)
        assert _wait_until(lambda: len(calls) == 3)
    finally:
        unsubscribe()

    # every record was written to the file despite the raising subscriber
    assert len(op.path.read_text().splitlines()) == 3
    assert capsys.readouterr().err.count("event subscriber raised") == 1


def test_slow_subscriber_drops_oldest_without_blocking_emit(tmp_path, capsys):
    entered = threading.Event()
    release = threading.Event()
    received = []

    def slow_subscriber(record):
        entered.set()
        release.wait(timeout=10)
        received.append(record.seq)

    unsubscribe = subscribe(slow_subscriber, max_pending=2)
    try:
        op = OperationLog.start("status", tmp_path / "events")  # seq 0: picked up by the worker
        assert entered.wait(timeout=5)  # the worker is now stuck inside the callback
        started = time.monotonic()
        for i in range(6):
            op.emit("step_completed", f"step {i}")  # seq 1..6 offered; only 2 may stay pending
        assert time.monotonic() - started < 1.0  # emit never blocked on the stuck subscriber
        release.set()
        assert _wait_until(lambda: len(received) == 3)  # seq 0 + the last 2 pending
    finally:
        unsubscribe()

    assert received[0] == 0
    assert received[1:] == [5, 6]
    assert "queue full" in capsys.readouterr().err
    # the file has every record regardless of bus drops
    assert len(op.path.read_text().splitlines()) == 7


def test_unsubscribe_stops_delivery_and_is_idempotent(tmp_path):
    received = []
    unsubscribe = subscribe(received.append)
    op = OperationLog.start("status", tmp_path / "events")
    assert _wait_until(lambda: len(received) == 1)
    unsubscribe()
    unsubscribe()
    op.finish(ok=True)
    time.sleep(0.05)
    assert len(received) == 1


def test_failed_file_write_does_not_publish(tmp_path):
    received = []
    unsubscribe = subscribe(received.append)
    try:
        blocked = tmp_path / "not_a_dir"
        blocked.write_text("i am a file, not a directory")
        op = OperationLog.start("status", blocked / "events")
        op.emit("step_started", "never written")
        time.sleep(0.05)
    finally:
        unsubscribe()
    assert received == []


def test_multiple_subscribers_fan_out_independently(tmp_path):
    first, second = [], []
    unsub_first = subscribe(first.append)
    unsub_second = subscribe(second.append)
    try:
        op = OperationLog.start("status", tmp_path / "events")
        op.finish(ok=True)
        assert _wait_until(lambda: len(first) == 2 and len(second) == 2)
    finally:
        unsub_first()
        unsub_second()
    assert [r.seq for r in first] == [r.seq for r in second] == [0, 1]
    assert not events_module._subscribers
