import json

import pytest

from nctl_core.artifacts import OperationArtifacts
from nctl_core.events import OperationLog
from nctl_core.operations_index import (
    OperationIndexError,
    list_operations,
    load_operation,
    read_events,
    validate_operation_id,
)


def _finished_op(log_dir, op="reconcile", ok=True, message="converged"):
    log = OperationLog.start(op, log_dir)
    log.emit("step_completed", "did a thing", ok=True)
    log.emit("finished", message, ok=ok)
    return log


def test_load_operation_finished_reads_state_from_last_record(tmp_path):
    log = _finished_op(tmp_path, ok=True, message="converged")
    record = load_operation(tmp_path, log.operation_id)
    assert record is not None
    assert record.op == "reconcile"
    assert record.state == "finished"
    assert record.ok is True
    assert record.result == "converged"
    assert record.event_count == 3
    assert record.last_seq == 2
    assert record.corrupt_lines == 0
    assert record.log_path == str(log.path)
    assert record.artifact_dir is None
    assert record.started_at is not None and record.updated_at is not None
    assert record.started_at <= record.updated_at


def test_load_operation_running_when_no_finished_record(tmp_path):
    log = OperationLog.start("drift", tmp_path)
    log.emit("step_started", "fetching")
    record = load_operation(tmp_path, log.operation_id)
    assert record.state == "running"
    assert record.ok is None
    assert record.result is None


def test_load_operation_lists_artifacts_from_operation_directory(tmp_path):
    log = _finished_op(tmp_path)
    artifacts = OperationArtifacts.create(tmp_path, log.operation_id)
    artifacts.write_json("plan.json", {"actions": []})
    artifacts.write_json("round-00/drift-before.json", {"targets": []})
    record = load_operation(tmp_path, log.operation_id)
    assert record.artifact_dir == str(artifacts.root)
    names = [a.name for a in record.artifacts]
    assert names == ["plan.json", "round-00/drift-before.json"]
    assert all(a.size_bytes > 0 for a in record.artifacts)


def test_load_operation_with_artifact_dir_but_no_log(tmp_path):
    operation_id = "01JZZZZZZZZZZZZZZZZZZZZZZZ"
    OperationArtifacts.create(tmp_path, operation_id).write_json("plan.json", {})
    record = load_operation(tmp_path, operation_id)
    assert record is not None
    assert record.op is None
    assert record.state == "no_events"
    assert record.log_path is None
    assert [a.name for a in record.artifacts] == ["plan.json"]


def test_load_operation_unknown_returns_none(tmp_path):
    assert load_operation(tmp_path, "01JZZZZZZZZZZZZZZZZZZZZZZZ") is None


def test_malformed_operation_id_is_rejected(tmp_path):
    for bad in ("../escape", "short", "01jzzzzzzzzzzzzzzzzzzzzzzz", "01JZZZZZZZZZZZZZZZZZZZZZZ/"):
        with pytest.raises(OperationIndexError):
            validate_operation_id(bad)
        with pytest.raises(OperationIndexError):
            load_operation(tmp_path, bad)


def test_corrupted_and_partial_lines_are_counted_and_skipped(tmp_path):
    log = _finished_op(tmp_path)
    content = log.path.read_text()
    lines = content.splitlines()
    # inject garbage between valid lines plus a truncated final write
    lines.insert(1, "{not json at all")
    lines.insert(3, json.dumps({"unexpected": "shape"}))
    log.path.write_text("\n".join(lines) + "\n" + '{"ts": "2026-07-17T', )
    record = load_operation(tmp_path, log.operation_id)
    assert record.event_count == 3
    assert record.corrupt_lines == 3
    assert record.state == "finished"

    events, corrupt = read_events(tmp_path, log.operation_id)
    assert [e.seq for e in events] == [0, 1, 2]
    assert corrupt == 3


def test_read_events_after_seq_cursor(tmp_path):
    log = _finished_op(tmp_path)
    events, _ = read_events(tmp_path, log.operation_id, after_seq=0)
    assert [e.seq for e in events] == [1, 2]
    events, _ = read_events(tmp_path, log.operation_id, after_seq=2)
    assert events == []


def test_read_events_missing_file_is_empty(tmp_path):
    assert read_events(tmp_path, "01JZZZZZZZZZZZZZZZZZZZZZZZ") == ([], 0)


def test_list_operations_newest_first_with_limit(tmp_path):
    ids = [_finished_op(tmp_path).operation_id for _ in range(3)]
    records = list_operations(tmp_path)
    assert [r.operation_id for r in records] == sorted(ids, reverse=True)
    limited = list_operations(tmp_path, limit=2)
    assert [r.operation_id for r in limited] == sorted(ids, reverse=True)[:2]


def test_list_operations_ignores_unrelated_entries(tmp_path):
    _finished_op(tmp_path)
    (tmp_path / "notes.txt").write_text("not an operation")
    (tmp_path / "random-dir").mkdir()
    (tmp_path / "lowercase.jsonl").write_text("{}")
    records = list_operations(tmp_path)
    assert len(records) == 1


def test_list_operations_missing_dir_is_empty(tmp_path):
    assert list_operations(tmp_path / "does-not-exist") == []


def test_list_operations_over_real_phase4_layout(tmp_path):
    """An applying-reconcile-shaped fixture: JSONL + plan/round artifacts like Phase 4 writes."""
    log = OperationLog.start("reconcile", tmp_path)
    artifacts = OperationArtifacts.create(tmp_path, log.operation_id)
    artifacts.write_json("plan.json", {"actions": [{"action_id": "a1"}]})
    artifacts.write_json("round-00/drift-before.json", {"summary": {"drifting": 1}})
    log.emit("plan_created", "plan built", drift_fingerprint="abc", action_count=1)
    log.emit("actuation_completed", "dnsmasq deployed", success=True, target_slugs=["agdnsmasq"],
             claimed_diff_codes=["dnsmasq_stale"], requires_observation=True)
    log.emit("observation_completed", "fresh facts ingested", ok=True)
    artifacts.write_json("round-00/drift-final.json", {"summary": {"converged": 1}})
    log.emit("drift_resolved", "reconcile converged", state="converged")
    log.emit("finished", "converged", ok=True)

    record = list_operations(tmp_path)[0]
    assert record.op == "reconcile"
    assert record.state == "finished"
    assert record.ok is True
    assert record.result == "converged"
    assert {a.name for a in record.artifacts} == {
        "plan.json",
        "round-00/drift-before.json",
        "round-00/drift-final.json",
    }
