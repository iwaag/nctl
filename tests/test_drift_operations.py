from __future__ import annotations

import json
from pathlib import Path

from nctl_core.drift.operations import latest_operation_timestamp_for_target


def write_events(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")


def test_no_events_dir_returns_none(tmp_path):
    assert latest_operation_timestamp_for_target(tmp_path / "nope", "agweb") is None


def test_finds_timestamp_from_target_hosts_list(tmp_path):
    write_events(
        tmp_path / "op1.jsonl",
        [
            {"ts": "2026-07-15T10:00:00+00:00", "event": "started", "data": {}},
            {"ts": "2026-07-15T10:05:00+00:00", "event": "apply_started", "data": {"target_hosts": ["agweb", "agdb"]}},
        ],
    )

    result = latest_operation_timestamp_for_target(tmp_path, "agweb")

    assert result.isoformat() == "2026-07-15T10:05:00+00:00"


def test_returns_none_when_no_event_mentions_target(tmp_path):
    write_events(
        tmp_path / "op1.jsonl",
        [{"ts": "2026-07-15T10:00:00+00:00", "event": "apply_started", "data": {"target_hosts": ["agdb"]}}],
    )

    assert latest_operation_timestamp_for_target(tmp_path, "agweb") is None


def test_takes_the_latest_across_multiple_operation_files(tmp_path):
    write_events(
        tmp_path / "op1.jsonl",
        [{"ts": "2026-07-15T09:00:00+00:00", "event": "apply_started", "data": {"target_hosts": ["agweb"]}}],
    )
    write_events(
        tmp_path / "op2.jsonl",
        [{"ts": "2026-07-15T11:00:00+00:00", "event": "apply_started", "data": {"target_hosts": ["agweb"]}}],
    )

    result = latest_operation_timestamp_for_target(tmp_path, "agweb")

    assert result.isoformat() == "2026-07-15T11:00:00+00:00"


def test_tolerates_malformed_lines_and_missing_timestamps(tmp_path):
    path = tmp_path / "op1.jsonl"
    path.write_text(
        "{not json\n"
        + json.dumps({"event": "apply_started", "data": {"target_hosts": ["agweb"]}})
        + "\n"
        + json.dumps({"ts": "2026-07-15T12:00:00+00:00", "event": "apply_started", "data": {"target_hosts": ["agweb"]}})
        + "\n"
    )

    result = latest_operation_timestamp_for_target(tmp_path, "agweb")

    assert result.isoformat() == "2026-07-15T12:00:00+00:00"


def test_matches_target_nested_inside_a_dict_value(tmp_path):
    write_events(
        tmp_path / "op1.jsonl",
        [
            {
                "ts": "2026-07-15T10:00:00+00:00",
                "event": "step_completed",
                "data": {"host_result": {"hostname": "agweb"}},
            }
        ],
    )

    result = latest_operation_timestamp_for_target(tmp_path, "agweb")

    assert result is not None
