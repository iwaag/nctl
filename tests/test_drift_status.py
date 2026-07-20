from __future__ import annotations

import json
from datetime import datetime, timezone

from nctl_core.drift.model import DiffRecord, Severity, Status, Target
from nctl_core.drift.status import derive_status


def _diff(code: str, severity: Severity = Severity.ERROR) -> DiffRecord:
    return DiffRecord(target=Target(kind="node", slug="agweb"), code=code, severity=severity, message="test")


def _write_success(path, ts: str, target_slugs: list[str], claimed: list[str]) -> None:
    path.write_text(
        json.dumps(
            {
                "ts": ts,
                "event": "actuation_completed",
                "data": {
                    "target_slugs": target_slugs,
                    "success": True,
                    "requires_observation": True,
                    "claimed_diff_codes": claimed,
                },
            }
        )
        + "\n"
    )


def test_no_error_diffs_is_converged():
    status = derive_status([], target_slug="agweb", observed_at=None, events_dir=None)
    assert status == Status.CONVERGED


def test_warning_and_info_only_is_converged():
    status = derive_status(
        [_diff("active_placement_not_applied", Severity.WARNING), _diff("derived_value_provenance", Severity.INFO)],
        target_slug="agweb",
        observed_at=None,
        events_dir=None,
    )
    assert status == Status.CONVERGED


def test_unknown_code_is_unknown():
    status = derive_status([_diff("no_realized_device")], target_slug="agweb", observed_at=None, events_dir=None)
    assert status == Status.UNKNOWN


def test_non_unknown_error_is_drifting():
    status = derive_status([_diff("missing_mac_address")], target_slug="agweb", observed_at=None, events_dir=None)
    assert status == Status.DRIFTING


def test_converging_when_claimed_actuation_is_newer_than_observation(tmp_path):
    _write_success(tmp_path / "op1.jsonl", "2026-07-15T12:00:00+00:00", ["agweb"], ["missing_mac_address"])
    observed_at = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)

    status = derive_status(
        [_diff("missing_mac_address")], target_slug="agweb", observed_at=observed_at, events_dir=tmp_path
    )

    assert status == Status.CONVERGING


def test_drifting_when_actuation_is_older_than_observation(tmp_path):
    _write_success(tmp_path / "op1.jsonl", "2026-07-15T08:00:00+00:00", ["agweb"], ["missing_mac_address"])
    observed_at = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)

    status = derive_status(
        [_diff("missing_mac_address")], target_slug="agweb", observed_at=observed_at, events_dir=tmp_path
    )

    assert status == Status.DRIFTING


def test_drifting_when_no_events_dir_configured():
    status = derive_status([_diff("missing_mac_address")], target_slug="agweb", observed_at=None, events_dir=None)
    assert status == Status.DRIFTING


def test_generic_event_mentioning_slug_never_produces_converging(tmp_path):
    """A `step_started` (or any non-`actuation_completed` event) that mentions the target
    slug anywhere in its data must not be mistaken for an in-flight change."""
    (tmp_path / "op1.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-07-15T12:00:00+00:00",
                "event": "step_started",
                "data": {"target_hosts": ["agweb"]},
            }
        )
        + "\n"
    )
    observed_at = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)

    status = derive_status(
        [_diff("missing_mac_address")], target_slug="agweb", observed_at=observed_at, events_dir=tmp_path
    )

    assert status == Status.DRIFTING


def test_failed_actuation_never_produces_converging(tmp_path):
    (tmp_path / "op1.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-07-15T12:00:00+00:00",
                "event": "actuation_completed",
                "data": {
                    "target_slugs": ["agweb"],
                    "success": False,
                    "requires_observation": True,
                    "claimed_diff_codes": ["missing_mac_address"],
                },
            }
        )
        + "\n"
    )
    observed_at = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)

    status = derive_status(
        [_diff("missing_mac_address")], target_slug="agweb", observed_at=observed_at, events_dir=tmp_path
    )

    assert status == Status.DRIFTING


def test_unclaimed_error_alongside_a_claimed_one_stays_drifting(tmp_path):
    """One claimed + one unclaimed error: the unclaimed error must keep the whole target
    out of `converging` even though the claimed one would qualify alone."""
    _write_success(tmp_path / "op1.jsonl", "2026-07-15T12:00:00+00:00", ["agweb"], ["missing_mac_address"])
    observed_at = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)

    status = derive_status(
        [_diff("missing_mac_address"), _diff("service_not_running")],
        target_slug="agweb",
        observed_at=observed_at,
        events_dir=tmp_path,
    )

    assert status == Status.DRIFTING


def test_successful_actuation_followed_by_newer_observation_is_not_converging(tmp_path):
    _write_success(tmp_path / "op1.jsonl", "2026-07-15T09:00:00+00:00", ["agweb"], ["missing_mac_address"])
    observed_at = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)

    status = derive_status(
        [_diff("missing_mac_address")], target_slug="agweb", observed_at=observed_at, events_dir=tmp_path
    )

    assert status == Status.DRIFTING


def test_phase1_local_composition_error_is_drifting_not_unknown():
    # A Group C code (Phase 1) means "we have the data and it's invalid",
    # not "we lack actual data" -- it must resolve to drifting, unlike the
    # existing evidence-gap codes in UNKNOWN_CODES.
    status = derive_status([_diff("unknown_profile")], target_slug="agweb", observed_at=None, events_dir=None)
    assert status == Status.DRIFTING


def test_active_placement_not_applied_warning_alone_is_converged():
    status = derive_status(
        [_diff("active_placement_not_applied", Severity.WARNING)],
        target_slug="agplanned", observed_at=None, events_dir=None,
    )
    assert status == Status.CONVERGED
