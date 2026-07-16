"""Reads the event log directory to support the `converging` status rule
(Phase 2 Step 3, tightened in Phase 4 Step 4).

A target counts as having an in-flight, observation-pending change only when
the *chronologically latest* `actuation_completed` event that names it in
`data.target_slugs` is itself a successful, observation-requiring actuation
carrying `data.claimed_diff_codes`. A generic event that merely happens to
mention the slug (e.g. `step_started`), a failed/cancelled actuation, or a
later failure that supersedes an earlier success must never produce
`converging` — see `docs/event-log.md` and `p4/plan.md` Step 4.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple


class ConvergentActuation(NamedTuple):
    ts: datetime
    claimed_diff_codes: frozenset[str]


def latest_convergent_actuation_for_target(
    events_dir: Path, target_slug: str
) -> ConvergentActuation | None:
    """Return the latest qualifying actuation for `target_slug`, if any.

    Only the single chronologically latest `actuation_completed` event that
    names `target_slug` in `data.target_slugs` is considered. If that event
    is not a successful, observation-requiring actuation with a well-formed
    `claimed_diff_codes` list, this returns `None` even if an earlier event
    for the same target would have qualified — a later failure/cancellation
    always invalidates an earlier success.
    """

    if not events_dir.is_dir():
        return None

    latest_ts: datetime | None = None
    latest_data: dict[str, Any] | None = None

    for path in sorted(events_dir.glob("*.jsonl")):
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("event") != "actuation_completed":
                continue
            data = record.get("data")
            if not isinstance(data, dict):
                continue
            target_slugs = data.get("target_slugs")
            if not isinstance(target_slugs, list) or target_slug not in target_slugs:
                continue
            ts_raw = record.get("ts")
            if not isinstance(ts_raw, str):
                continue
            try:
                ts = datetime.fromisoformat(ts_raw)
            except ValueError:
                continue
            if latest_ts is None or ts > latest_ts:
                latest_ts = ts
                latest_data = data

    if latest_ts is None or latest_data is None:
        return None
    if latest_data.get("success") is not True or latest_data.get("requires_observation") is not True:
        return None
    claimed = latest_data.get("claimed_diff_codes")
    if not isinstance(claimed, list) or not claimed or not all(isinstance(c, str) for c in claimed):
        return None
    return ConvergentActuation(ts=latest_ts, claimed_diff_codes=frozenset(claimed))
