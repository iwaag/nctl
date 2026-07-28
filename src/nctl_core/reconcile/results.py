"""Reconcile result and durable-envelope contracts."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .model import PlanScope


RECONCILE_SCHEMA = "nctl.reconcile.v2"


class ActionResult(BaseModel):
    action_id: str
    reconciler_id: str
    action_kind: str
    target_slugs: list[str] = Field(default_factory=list)
    success: bool
    detail: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    # ipam_policy p6 Step 4: a partial reconcile_ipam Job run (one expected
    # endpoint applied, another still conflicts) is not `success`, but the
    # applied mutation still happened and must count toward `progress_made`
    # rather than being indistinguishable from an action that mutated
    # nothing at all.
    mutated: bool = False


class RoundSummary(BaseModel):
    round: int
    drift_fingerprint: str
    actions: list[ActionResult] = Field(default_factory=list)
    # fix_sshkey3 Step 2 (contract item 7): the post-regeneration production
    # SSH scan's own SshPreflightEntry records (phase/route/port/generation/
    # fingerprints), captured per round regardless of outcome -- this is the
    # artifact evidence a live verification proves the exact scan decision
    # from, not just the flattened enrollment-gate summary on `ReconcileData`.
    ssh_preflight: list[dict[str, Any]] = Field(default_factory=list)


class ReconcileData(BaseModel):
    operation_id: str
    mode: str
    scope: PlanScope
    state: str = "failed"
    event_log_path: str
    artifact_dir: str = ""
    plan_path: str = ""
    initial_drift_path: str = ""
    final_drift_path: str = ""
    rounds: list[RoundSummary] = Field(default_factory=list)
    manual_review: list[dict[str, Any]] = Field(default_factory=list)
    unsupported: list[dict[str, Any]] = Field(default_factory=list)
    summary: dict[str, int] = Field(default_factory=dict)
    scope_summary: dict[str, int] = Field(default_factory=dict)
    progress_made: bool = False
    # Controller-local SSH trust readiness (fix_sshkey Step 5, Design Decision 5/6):
    # informational alongside drift/action state, never itself a drift code or
    # Nautobot status. Each entry is one nctl_core.reconcile.ssh_preflight.SshPreflightEntry.
    ssh_preflight: list[dict[str, Any]] = Field(default_factory=list)
