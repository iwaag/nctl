from __future__ import annotations

import json
from pathlib import Path

from nctl_core.drift.operations import latest_convergent_actuation_for_target


def write_events(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")


def _actuation(ts: str, target_slugs: list[str], **data) -> dict:
    return {
        "ts": ts,
        "event": "actuation_completed",
        "data": {"target_slugs": target_slugs, **data},
    }


def _success(ts: str, target_slugs: list[str], claimed: list[str]) -> dict:
    return _actuation(
        ts, target_slugs, success=True, requires_observation=True, claimed_diff_codes=claimed
    )


def test_no_events_dir_returns_none(tmp_path):
    assert latest_convergent_actuation_for_target(tmp_path / "nope", "agweb") is None


def test_finds_successful_qualifying_actuation(tmp_path):
    write_events(
        tmp_path / "op1.jsonl",
        [
            {"ts": "2026-07-15T10:00:00+00:00", "event": "started", "data": {}},
            _success("2026-07-15T10:05:00+00:00", ["agweb", "agdb"], ["missing_mac_address"]),
        ],
    )

    result = latest_convergent_actuation_for_target(tmp_path, "agweb")

    assert result is not None
    assert result.ts.isoformat() == "2026-07-15T10:05:00+00:00"
    assert result.claimed_diff_codes == frozenset({"missing_mac_address"})


def test_returns_none_when_no_actuation_names_target(tmp_path):
    write_events(
        tmp_path / "op1.jsonl",
        [_success("2026-07-15T10:00:00+00:00", ["agdb"], ["missing_mac_address"])],
    )

    assert latest_convergent_actuation_for_target(tmp_path, "agweb") is None


def test_generic_event_mentioning_target_slug_is_ignored(tmp_path):
    """A step_started/step_completed (or any non-actuation_completed event) that happens to
    mention the slug must never be mistaken for an in-flight change (the Step 4 defect this
    tightening fixes)."""
    write_events(
        tmp_path / "op1.jsonl",
        [
            {
                "ts": "2026-07-15T10:00:00+00:00",
                "event": "step_started",
                "data": {"target_slugs": ["agweb"], "message": "agweb mentioned here"},
            }
        ],
    )

    assert latest_convergent_actuation_for_target(tmp_path, "agweb") is None


def test_failed_actuation_never_qualifies(tmp_path):
    write_events(
        tmp_path / "op1.jsonl",
        [_actuation("2026-07-15T10:00:00+00:00", ["agweb"], success=False, requires_observation=True, claimed_diff_codes=["missing_mac_address"])],
    )

    assert latest_convergent_actuation_for_target(tmp_path, "agweb") is None


def test_later_failure_invalidates_earlier_success(tmp_path):
    write_events(
        tmp_path / "op1.jsonl",
        [
            _success("2026-07-15T10:00:00+00:00", ["agweb"], ["missing_mac_address"]),
            _actuation(
                "2026-07-15T10:05:00+00:00",
                ["agweb"],
                success=False,
                requires_observation=True,
                claimed_diff_codes=["missing_mac_address"],
            ),
        ],
    )

    assert latest_convergent_actuation_for_target(tmp_path, "agweb") is None


def test_ledger_only_action_without_requires_observation_does_not_qualify(tmp_path):
    write_events(
        tmp_path / "op1.jsonl",
        [
            _actuation(
                "2026-07-15T10:00:00+00:00",
                ["agweb"],
                success=True,
                requires_observation=False,
                claimed_diff_codes=["actual_node_not_linked"],
            )
        ],
    )

    assert latest_convergent_actuation_for_target(tmp_path, "agweb") is None


def test_takes_the_latest_across_multiple_operation_files(tmp_path):
    write_events(
        tmp_path / "op1.jsonl",
        [_success("2026-07-15T09:00:00+00:00", ["agweb"], ["missing_mac_address"])],
    )
    write_events(
        tmp_path / "op2.jsonl",
        [_success("2026-07-15T11:00:00+00:00", ["agweb"], ["missing_mac_address"])],
    )

    result = latest_convergent_actuation_for_target(tmp_path, "agweb")

    assert result.ts.isoformat() == "2026-07-15T11:00:00+00:00"


def test_tolerates_malformed_lines_and_missing_timestamps(tmp_path):
    path = tmp_path / "op1.jsonl"
    path.write_text(
        "{not json\n"
        + json.dumps({"event": "actuation_completed", "data": {"target_slugs": ["agweb"]}})
        + "\n"
        + json.dumps(_success("2026-07-15T12:00:00+00:00", ["agweb"], ["missing_mac_address"]))
        + "\n"
    )

    result = latest_convergent_actuation_for_target(tmp_path, "agweb")

    assert result is not None
    assert result.ts.isoformat() == "2026-07-15T12:00:00+00:00"


def test_missing_claimed_diff_codes_does_not_qualify(tmp_path):
    write_events(
        tmp_path / "op1.jsonl",
        [_actuation("2026-07-15T10:00:00+00:00", ["agweb"], success=True, requires_observation=True)],
    )

    assert latest_convergent_actuation_for_target(tmp_path, "agweb") is None
