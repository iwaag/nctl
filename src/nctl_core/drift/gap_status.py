"""One deterministic owner for evaluation-gap status precedence."""

from __future__ import annotations

from typing import Any


def status_from_gaps(gaps: list[dict[str, Any]]) -> str:
    severities = {gap.get("severity") for gap in gaps}
    for severity in ("conflict", "missing", "partial", "needs_review", "unknown"):
        if severity in severities:
            return severity
    return "satisfied"
