"""Drift fingerprinting for the bounded re-plan loop (Phase 4 Step 5, Decision 3).

`nctl reconcile --yes` refetches and re-plans after each boundary; a fingerprint
of the *remaining error diffs* lets the bounded executor (Step 7) detect a
no-progress round and stop instead of looping. Only error-severity diffs
count -- warning/info diagnostics (`ingest_lag`, `intent_effect_summary`,
...) can fluctuate round to round without representing unresolved drift, and
including them would make the fingerprint change even when nothing
actionable is left.
"""

from __future__ import annotations

from nctl_core.drift.model import DiffRecord, Severity
from nctl_core.production.contract import canonical_json_digest


def compute_drift_fingerprint(diffs: list[DiffRecord]) -> str:
    rows = [
        {
            "kind": diff.target.kind,
            "slug": diff.target.slug,
            "name": diff.target.name,
            "id": diff.target.id,
            "code": diff.code,
        }
        for diff in diffs
        if diff.severity == Severity.ERROR
    ]
    rows.sort(key=lambda row: (row["kind"], row["slug"] or "", row["name"] or "", row["id"] or "", row["code"]))
    return canonical_json_digest(rows)
