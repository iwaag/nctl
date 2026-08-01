"""`nctl workspaces`: one row per declared workspace (creative_workspace p2 Step 3).

Reuses `drift_render.fetch_and_compute_drift` for the snapshot/`generated_at`
(exactly the `relations`/`drift` precedent) and calls
`workspace_evaluation.evaluate_all_workspaces` -- the identical pure function
`drift.comparators.workspace_intent_matching` calls over the same
`SourceSnapshot` -- directly, rather than re-parsing `DriftResult`'s diffs.
Same deterministic inputs, same pure function, so `nctl drift` and `nctl
workspaces` cannot disagree about a workspace's convergent state by
construction (the invariant `relations_render.py` established for bindings).
The `activity_class` this view also renders is the one field `drift`
deliberately never surfaces (roadmap hard rule 2).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from nctl_core.config import Config
from nctl_core.drift.evaluation_snapshot import parse_now
from nctl_core.drift.workspace_evaluation import WorkspaceEvaluation, evaluate_all_workspaces
from nctl_core.output import Envelope, EnvelopeError
from nctl_core.sources.desired import DesiredWorkspace
from nctl_core.sources.snapshot import SourceSnapshot

from .drift_render import fetch_and_compute_drift

WORKSPACES_SCHEMA = "nctl.workspaces.v1"

_IDENTITY_UNKNOWN_REASON_TEXT = {
    "not_a_git_repository": "not a git repo",
    "remote_url_missing": "no remote configured",
}


class WorkspaceRow(BaseModel):
    slug: str
    name: str
    node: str
    desired_presence: str
    presence: str
    identity: str
    identity_reason: str | None = None
    activity_class: str | None = None
    activity_reasons: dict[str, Any] = {}
    freshness: str
    checked_at: str | None = None
    gap_codes: list[str] = []


class WorkspacesData(BaseModel):
    generated_at: str = ""
    rows: list[WorkspaceRow] = []
    summary: dict[str, int] = {}


def build_workspaces(cfg: Config, *, host: str | None = None) -> Envelope[WorkspacesData]:
    fetched = fetch_and_compute_drift(cfg)
    if isinstance(fetched, EnvelopeError):
        return Envelope.build(WORKSPACES_SCHEMA, WorkspacesData(), [fetched])
    snapshot, _result, generated_at = fetched
    data = render_workspaces_data(
        snapshot, generated_at, host=host,
        stale_after_hours=cfg.reconcile.workspace_observation_max_age_hours,
    )
    return Envelope.build(WORKSPACES_SCHEMA, data, [])


def render_workspaces_data(
    snapshot: SourceSnapshot,
    generated_at: str,
    *,
    host: str | None = None,
    stale_after_hours: int = 24,
) -> WorkspacesData:
    """Pure projection: takes a `SourceSnapshot` and its `generated_at` (both
    from `fetch_and_compute_drift`) and derives the workspace row list. Never
    touches Nautobot itself."""

    evaluations = evaluate_all_workspaces(
        snapshot.desired.workspaces,
        snapshot.desired.nodes,
        snapshot.actual.devices,
        now=parse_now(generated_at),
        stale_after_hours=stale_after_hours,
    )
    rows = [_build_row(workspace, evaluations[workspace.id]) for workspace in snapshot.desired.workspaces]
    rows.sort(key=lambda row: row.slug)
    if host is not None:
        rows = [row for row in rows if row.node == host]
    return WorkspacesData(generated_at=generated_at, rows=rows, summary=_summary(rows))


def _build_row(workspace: DesiredWorkspace, evaluation: WorkspaceEvaluation) -> WorkspaceRow:
    gap_codes = [gap["code"] for gap in evaluation.gaps]
    observation = evaluation.observation

    presence = "unknown"
    if observation is not None:
        observed_present = observation.get("present")
        if observed_present is True:
            presence = "present"
        elif observed_present is False:
            presence = "missing"

    identity = "unknown"
    identity_reason: str | None = None
    if "workspace_identity_mismatch" in gap_codes:
        identity = "mismatch"
    elif "workspace_identity_unknown" in gap_codes:
        identity_reason = next(
            gap.get("reason") for gap in evaluation.gaps if gap["code"] == "workspace_identity_unknown"
        )
    elif (
        evaluation.desired_presence == "present"
        and observation is not None
        and observation.get("present") is True
    ):
        identity = "matched"

    if "workspace_observation_stale" in gap_codes:
        freshness = "stale"
    elif "workspace_observation_missing" in gap_codes:
        freshness = "missing"
    elif observation is not None:
        freshness = "fresh"
    else:
        freshness = "not_observed"

    return WorkspaceRow(
        slug=workspace.slug,
        name=workspace.name,
        node=workspace.node_slug,
        desired_presence=workspace.desired_presence,
        presence=presence,
        identity=identity,
        identity_reason=identity_reason,
        activity_class=evaluation.activity_class,
        activity_reasons=evaluation.activity_reasons,
        freshness=freshness,
        checked_at=observation.get("checked_at") if observation is not None else None,
        gap_codes=gap_codes,
    )


def _summary(rows: list[WorkspaceRow]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for row in rows:
        key = "drifting" if row.gap_codes else "converged"
        summary[key] = summary.get(key, 0) + 1
    return summary


def render_workspaces_text(envelope: Envelope[WorkspacesData]) -> str:
    if not envelope.ok:
        return "\n".join(f"error [{err.code}]: {err.message}" for err in envelope.errors)

    data = envelope.data
    lines: list[str] = []
    for row in data.rows:
        identity_text = row.identity
        if row.identity == "unknown" and row.identity_reason:
            identity_text = f"unknown ({_IDENTITY_UNKNOWN_REASON_TEXT.get(row.identity_reason, row.identity_reason)})"
        activity_text = row.activity_class or "unknown"
        lines.append(
            f"{row.slug} @{row.node}  presence={row.presence}  identity={identity_text}  "
            f"activity={activity_text}  freshness={row.freshness}"
        )
        if row.gap_codes:
            lines.append("    gaps: " + ", ".join(row.gap_codes))

    if not data.rows:
        lines.append("(no declared workspaces)")

    summary_line = " ".join(f"{state}={count}" for state, count in sorted(data.summary.items()))
    lines.append(f"summary: {summary_line}" if summary_line else "summary: (no workspaces)")
    return "\n".join(lines)
