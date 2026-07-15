"""Shared context passed to every comparator (Phase 2 Step 3).

Kept separate from `SourceSnapshot` because it carries things that are not
"a source" in the Step 1 sense: `generated_at` is when this drift run started
(not when any source was fetched), `profiles` is the validated
deployment-profiles map the production-policy comparator needs (loading it is
a filesystem read against `[ansible] playbook_dir`, not a Nautobot/dump
source), and `events_dir` is only needed by the `converging` status rule.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DriftContext:
    generated_at: str
    profiles: dict[str, Any] = field(default_factory=dict)
    events_dir: Path | None = None
