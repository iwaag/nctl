"""Reads the Phase 0 event log directory to support the `converging` status
rule (Phase 2 Step 3).

An operation counts as "targeting" a target slug when that slug appears
anywhere in one of its events' `data` payload (for example `apply dnsmasq`'s
`target_hosts` list — see `dnsmasq_apply.py`). This will rarely match
anything until Phase 4 registers more reconcilers that emit per-node events;
the lookup and the `converging` status are defined now so the drift schema
does not need to grow a new status once Phase 4 lands.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


def latest_operation_timestamp_for_target(events_dir: Path, target_slug: str) -> datetime | None:
    """Return the newest event timestamp across all operation logs mentioning `target_slug`."""

    if not events_dir.is_dir():
        return None

    latest: datetime | None = None
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
            if not _mentions_target(record.get("data", {}), target_slug):
                continue
            ts = record.get("ts")
            if not isinstance(ts, str):
                continue
            try:
                parsed = datetime.fromisoformat(ts)
            except ValueError:
                continue
            if latest is None or parsed > latest:
                latest = parsed
    return latest


def _mentions_target(data: Any, target_slug: str) -> bool:
    if isinstance(data, str):
        return data == target_slug
    if isinstance(data, list):
        return any(_mentions_target(item, target_slug) for item in data)
    if isinstance(data, dict):
        return any(_mentions_target(value, target_slug) for value in data.values())
    return False
