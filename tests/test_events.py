import json
import re

from nctl_core.events import OperationLog, generate_ulid

ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


def test_generate_ulid_shape_and_uniqueness():
    ids = {generate_ulid() for _ in range(50)}
    assert len(ids) == 50
    for ulid in ids:
        assert ULID_RE.match(ulid), ulid


def test_start_writes_started_event(tmp_path):
    log_dir = tmp_path / "events"
    op = OperationLog.start("status", log_dir)
    lines = op.path.read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "started"
    assert record["op"] == "status"
    assert record["operation_id"] == op.operation_id
    assert record["seq"] == 0


def test_seq_is_monotonic(tmp_path):
    op = OperationLog.start("status", tmp_path / "events")
    op.emit("step_started", "a")
    op.emit("step_completed", "a done")
    op.finish(ok=True)
    lines = op.path.read_text().splitlines()
    records = [json.loads(line) for line in lines]
    assert [r["seq"] for r in records] == [0, 1, 2, 3]
    assert records[-1]["event"] == "finished"
    assert records[-1]["data"]["ok"] is True


def test_emit_data_kwargs_land_in_data_field(tmp_path):
    op = OperationLog.start("status", tmp_path / "events")
    op.emit("step_completed", "nautobot checked", ok=False, host_count=2)
    record = json.loads(op.path.read_text().splitlines()[-1])
    assert record["data"] == {"ok": False, "host_count": 2}


def test_write_failure_does_not_raise(tmp_path):
    blocked = tmp_path / "not_a_dir"
    blocked.write_text("i am a file, not a directory")
    op = OperationLog.start("status", blocked / "events")
    # start() itself must not raise even though log_dir.mkdir() will fail.
    op.emit("step_started", "still works")
