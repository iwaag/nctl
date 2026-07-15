from __future__ import annotations

import json
from datetime import datetime, timezone

from nctl_core.drift.model import DiffRecord, Severity, Status, Target
from nctl_core.drift.status import derive_status


def _diff(code: str, severity: Severity = Severity.ERROR) -> DiffRecord:
    return DiffRecord(target=Target(kind="node", slug="agweb"), code=code, severity=severity, message="test")


def test_no_error_diffs_is_converged():
    status = derive_status([], target_slug="agweb", observed_at=None, events_dir=None)
    assert status == Status.CONVERGED


def test_warning_and_info_only_is_converged():
    status = derive_status(
        [_diff("desired_actual_os_mismatch", Severity.WARNING), _diff("ingest_lag", Severity.INFO)],
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


def test_converging_when_operation_is_newer_than_observation(tmp_path):
    (tmp_path / "op1.jsonl").write_text(
        json.dumps({"ts": "2026-07-15T12:00:00+00:00", "event": "apply_started", "data": {"target_hosts": ["agweb"]}})
        + "\n"
    )
    observed_at = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)

    status = derive_status(
        [_diff("missing_mac_address")], target_slug="agweb", observed_at=observed_at, events_dir=tmp_path
    )

    assert status == Status.CONVERGING


def test_drifting_when_operation_is_older_than_observation(tmp_path):
    (tmp_path / "op1.jsonl").write_text(
        json.dumps({"ts": "2026-07-15T08:00:00+00:00", "event": "apply_started", "data": {"target_hosts": ["agweb"]}})
        + "\n"
    )
    observed_at = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)

    status = derive_status(
        [_diff("missing_mac_address")], target_slug="agweb", observed_at=observed_at, events_dir=tmp_path
    )

    assert status == Status.DRIFTING


def test_drifting_when_no_events_dir_configured():
    status = derive_status([_diff("missing_mac_address")], target_slug="agweb", observed_at=None, events_dir=None)
    assert status == Status.DRIFTING
