"""Drift computation core (Phase 2 Step 3): runs every registered comparator
over a `SourceSnapshot` and groups the resulting diff records into one
`TargetStatus` per target.

Every desired node and desired service is seeded into the result up front
(with zero diffs, hence `converged`) so a node or service nobody flagged
anything about still appears in `nctl.drift.v1`'s target list — the
roadmap's "AI can read just that to explain the current state" only holds if
silence means "nothing wrong", not "we forgot to report on it". Service
seeding is new in Step 4 (the `service_intent_matching` comparator is the
first thing that can produce a `kind="service"` diff). Comparator-produced
targets outside the desired-node/service sets (a `kind="device"` ingest-lag
diff for a dump with no matching desired node yet, or a `kind="global"`
production contract error) are added as their own targets rather than
dropped.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from nctl_core.sources.snapshot import SourceSnapshot

from . import comparators as _comparators  # noqa: F401  (import side effect: registers comparators)
from .context import DriftContext
from .model import DiffRecord, Status, Target
from .registry import run_comparators
from .status import derive_status


class TargetStatus(BaseModel):
    target: Target
    status: Status
    diffs: list[DiffRecord] = []


class DriftResult(BaseModel):
    summary: dict[str, int] = {}
    targets: list[TargetStatus] = []


def compute_drift(snapshot: SourceSnapshot, context: DriftContext) -> DriftResult:
    records = run_comparators(snapshot, context)
    targets = _group_by_target(records, snapshot, context)
    return DriftResult(summary=_summarize(targets), targets=targets)


def _target_key(target: Target) -> tuple[str, str]:
    return (target.kind, target.id or target.slug or target.name or "")


def _group_by_target(
    records: list[DiffRecord], snapshot: SourceSnapshot, context: DriftContext
) -> list[TargetStatus]:
    grouped: dict[tuple[str, str], list[DiffRecord]] = {}
    target_by_key: dict[tuple[str, str], Target] = {}

    for node in snapshot.desired.nodes:
        target = Target(kind="node", slug=node.slug, name=node.name, id=node.id)
        key = _target_key(target)
        grouped[key] = []
        target_by_key[key] = target

    for service in snapshot.desired.services:
        target = Target(kind="service", slug=service.slug, name=service.name, id=service.id)
        key = _target_key(target)
        grouped[key] = []
        target_by_key[key] = target

    for record in records:
        key = _target_key(record.target)
        grouped.setdefault(key, [])
        target_by_key.setdefault(key, record.target)
        grouped[key].append(record)

    devices_by_id = {device.id: device for device in snapshot.actual.devices}
    results = []
    for key in sorted(grouped):
        target = target_by_key[key]
        diffs = sorted(grouped[key], key=lambda record: record.code)
        observed_at = _observed_at_for(target, snapshot, devices_by_id)
        status = derive_status(
            diffs, target_slug=target.slug, observed_at=observed_at, events_dir=context.events_dir
        )
        results.append(TargetStatus(target=target, status=status, diffs=diffs))
    return results


def _observed_at_for(
    target: Target, snapshot: SourceSnapshot, devices_by_id: dict[str, Any]
) -> datetime | None:
    if target.kind == "service":
        node_by_id = {node.id: node for node in snapshot.desired.nodes}
        timestamps = []
        for placement in snapshot.desired.placements:
            if placement.service_id != target.id or placement.desired_state != "active":
                continue
            node = node_by_id.get(placement.node_id)
            device = devices_by_id.get(node.realized_device_id) if node and node.realized_device_id else None
            value = device.actual_facts().service_inventory_updated_at if device else None
            parsed = _parse_timestamp(value)
            if parsed is not None:
                timestamps.append(parsed)
        return max(timestamps) if timestamps else None
    if target.kind != "node":
        return None
    node = next((n for n in snapshot.desired.nodes if n.id == target.id), None)
    if node is None or not node.realized_device_id:
        return None
    device = devices_by_id.get(node.realized_device_id)
    if device is None:
        return None
    collected_at = device.actual_facts().collected_at
    if not collected_at:
        return None
    return _parse_timestamp(collected_at)


def _parse_timestamp(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


def _summarize(targets: list[TargetStatus]) -> dict[str, int]:
    summary = {status.value: 0 for status in Status}
    for target_status in targets:
        summary[target_status.status.value] += 1
    return summary
