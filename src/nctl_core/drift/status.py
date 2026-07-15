"""Per-target status derivation (Phase 2 Step 3): `converged` / `drifting` /
`converging` / `unknown`, computed from a target's diff records rather than
persisted anywhere — the roadmap treats the reconciliation engine's live
output as the single source of truth, so there is nothing to keep in sync.

- `unknown` — an error-severity diff whose code means "we don't have reliable
  actual data" (no realized object, unsupported actual type, missing/stale/
  invalid actual data) rather than "the data disagrees". These are exactly
  the skip reasons `production/composer.py::_host_actual_skip_reasons`, the
  Step 3 `node_existence` comparator, and (Step 4) the ported evaluation
  gap codes in `evaluation.NO_DATA_GAP_CODES` produce.
- `drifting` — any other error-severity diff: we have actual data, and it
  disagrees with desired state (or a global contract violation).
- `converging` — diffs exist, but an `nctl apply`/`reconcile` operation
  targeting this node is newer than the node's newest actual observation
  (read via `operations.latest_operation_timestamp_for_target`). Rare until
  Phase 4 registers reconcilers that emit per-node events.
- `converged` — no error-severity diffs (warning/info diffs still show up in
  the payload, they just don't change the status).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .evaluation import NO_DATA_GAP_CODES
from .model import DiffRecord, Severity, Status
from .operations import latest_operation_timestamp_for_target

UNKNOWN_CODES = frozenset(
    {
        "no_realized_device",
        "no_realized_object",
        "realized_device_missing",
        "realized_vm_missing",
        "unsupported_actual_type",
        "missing_actual_data",
        "stale_actual_data",
        "invalid_actual_timestamp",
        "nautobot_fetch_failed",
        "dump_parse_error",
    }
    | NO_DATA_GAP_CODES
)


def derive_status(
    records: list[DiffRecord],
    *,
    target_slug: str | None,
    observed_at: datetime | None,
    events_dir: Path | None,
) -> Status:
    error_records = [record for record in records if record.severity == Severity.ERROR]
    if not error_records:
        return Status.CONVERGED
    if any(record.code in UNKNOWN_CODES for record in error_records):
        return Status.UNKNOWN
    if target_slug and events_dir is not None:
        operation_ts = latest_operation_timestamp_for_target(events_dir, target_slug)
        if operation_ts is not None and (observed_at is None or operation_ts > observed_at):
            return Status.CONVERGING
    return Status.DRIFTING
