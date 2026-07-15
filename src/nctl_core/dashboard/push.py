"""Reconciliation-status write-back into nintent (Phase 3 Step 3).

Per the Phase 0-EX1 split, this is a REST write through the intent-catalog
ViewSets: each drift target's status and the payload's `generated_at` are
PATCHed onto its ledger row as `reconciliation_status` /
`reconciliation_checked_at` — documented on the nintent side (Step 4) as a
derived cache of the last nctl run; the drift engine stays the single source
of truth.

Decision 4: the push degrades, never fails. One target's failure doesn't
abort the rest, and every outcome aggregates into `StatusPushData` — the
caller (`build_dashboard`) treats it as warnings, not as `ok: false`.

Target → row mapping: `node` → `nodes/`, `service` → `services/` (the
DesiredService route is added to nintent in Step 4 alongside the fields; a
0.6.0 server 404s it, which lands in `skipped_no_row` — visible, not fatal).
Other kinds (`Target.kind` is an open set — global diagnostics etc.) have no
ledger row by construction and are counted as `skipped_no_row`. A target
without an id is looked up by slug (nodes) / name (services) first.
"""

from __future__ import annotations

from pydantic import BaseModel

from nctl_core.drift.engine import TargetStatus
from nctl_core.drift_render import DriftData
from nctl_core.nautobot import NautobotClient, NautobotError

INTENT_API_BASE = "/api/plugins/intent-catalog"
KIND_ROUTES = {"node": "nodes", "service": "services"}
KIND_LOOKUP_FIELDS = {"node": "slug", "service": "name"}


class StatusPushData(BaseModel):
    pushed: bool = False
    attempted: int = 0
    updated: int = 0
    skipped_no_row: int = 0
    failed: int = 0
    errors: list[str] = []


def push_statuses(client: NautobotClient, drift_data: DriftData) -> StatusPushData:
    result = StatusPushData(pushed=True)
    for target_status in drift_data.targets:
        result.attempted += 1
        _push_one(client, target_status, drift_data.generated_at, result)
    return result


def _push_one(
    client: NautobotClient, target_status: TargetStatus, checked_at: str, result: StatusPushData
) -> None:
    target = target_status.target
    label = target.slug or target.name or target.id or "?"
    route = KIND_ROUTES.get(target.kind)
    if route is None:
        result.skipped_no_row += 1
        return

    try:
        row_id = target.id or _lookup_row_id(client, target.kind, route, label)
        if row_id is None:
            result.skipped_no_row += 1
            return
        response = client.rest_patch(
            f"{INTENT_API_BASE}/{route}/{row_id}/",
            {"reconciliation_status": target_status.status.value, "reconciliation_checked_at": checked_at},
        )
    except NautobotError as exc:
        result.failed += 1
        result.errors.append(f"{target.kind} {label}: {exc}")
        return

    if response.status_code == 404:
        result.skipped_no_row += 1
    elif response.is_success:
        result.updated += 1
    else:
        result.failed += 1
        result.errors.append(f"{target.kind} {label}: HTTP {response.status_code}: {response.text[:200]}")


def _lookup_row_id(client: NautobotClient, kind: str, route: str, label: str) -> str | None:
    """Fallback for a target with no id: resolve the row by slug/name.

    Returns None (→ `skipped_no_row`) unless exactly one row matches.
    """
    field = KIND_LOOKUP_FIELDS[kind]
    response = client.rest_get(f"{INTENT_API_BASE}/{route}/", params={field: label, "limit": 2})
    if not response.is_success:
        return None
    results = response.json().get("results", [])
    if len(results) != 1:
        return None
    return results[0].get("id")
