"""Typed plan schema for `nctl.reconcile.plan.v1` (Phase 4 Step 5).

Mirrors `p4/plan.md`'s "Output and artifact contracts" section: a plan is a
serializable DAG of actions built before any mutation happens (Decision 2),
plus the manual-review/unsupported records the planner refused to automate.
Execution (Steps 6-7) consumes this schema; Step 5 only builds and validates
it.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

from nctl_core.drift.model import Target

PLAN_SCHEMA_VERSION = "nctl.reconcile.plan.v1"


class Classification(str, Enum):
    """The exactly-one-of-four bucket every selected diff resolves to (Decision 2)."""

    AUTOMATIC = "automatic"
    OBSERVATION = "observation"
    MANUAL_REVIEW = "manual_review"
    UNSUPPORTED = "unsupported"


class PlanScope(BaseModel):
    """What `nctl reconcile [HOST]` was asked to converge (Decision 1)."""

    kind: Literal["cluster", "host"]
    host_slug: str | None = None

    def label(self) -> str:
        return self.host_slug if self.kind == "host" and self.host_slug else "cluster"


class ReconcileAction(BaseModel):
    """One planned, not-yet-executed unit of mutation or observation."""

    id: str
    reconciler_id: str
    action_kind: str
    targets: list[Target]
    claimed_diff_codes: list[str]
    reason: str
    evidence: dict = Field(default_factory=dict)
    dependencies: list[str] = Field(default_factory=list)
    mutates: bool
    requires_observation: bool
    parameters: dict = Field(default_factory=dict)


class ManualReviewRecord(BaseModel):
    target: Target
    code: str
    severity: str
    message: str
    reason: str
    evidence: dict = Field(default_factory=dict)


class UnsupportedRecord(BaseModel):
    target: Target
    code: str
    severity: str
    message: str
    reason: str
    evidence: dict = Field(default_factory=dict)


class ReconcilePlan(BaseModel):
    schema_version: Literal["nctl.reconcile.plan.v1"] = PLAN_SCHEMA_VERSION
    scope: PlanScope
    drift_fingerprint: str
    drift_generated_at: str | None = None
    generated_at: datetime
    actions: list[ReconcileAction] = Field(default_factory=list)
    manual_review: list[ManualReviewRecord] = Field(default_factory=list)
    unsupported: list[UnsupportedRecord] = Field(default_factory=list)

    def has_blocking_findings(self) -> bool:
        return bool(self.manual_review) or bool(self.unsupported)
