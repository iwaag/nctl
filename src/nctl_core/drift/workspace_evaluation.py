"""Pure desired-workspace versus observed-workspace evaluation (creative_workspace p2 Step 2).

One evaluation per declared `DesiredWorkspace`, computed fresh from the same
`SourceSnapshot` every drift/`workspaces` run, never persisted. Mirrors
`binding_evaluation.py`'s shape (one small pure function, directly testable,
no registry/`DiffRecord` knowledge) more than `service_placement.py`'s (there
is no service-style multi-placement fan-out -- the roadmap fixes one
placement per workspace) -- convergent verdicts travel out as `gaps` (the
same `{"code": ..., **evidence}` shape `comparators.py` already knows how to
turn into `DiffRecord`s), while the informational `activity_class` travels in
its own field so a comparator can never accidentally promote it to a drift
code (roadmap hard rule 2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from nctl_core.sources.actual import ActualDevice
from nctl_core.sources.desired import DesiredNode, DesiredWorkspace

ACTIVITY_CLASSES = ("active_development", "behind_origin", "idle")


@dataclass(frozen=True)
class WorkspaceEvaluation:
    slug: str
    name: str
    node_id: str
    node_slug: str
    desired_presence: str
    realized_device_id: str | None
    observation: dict[str, Any] | None
    gaps: list[dict[str, Any]]
    activity_class: str | None
    activity_reasons: dict[str, Any]


def _age_hours(value: Any, now: datetime) -> float | None:
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (now.astimezone(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds() / 3600)


_SCP_STYLE = re.compile(r"^git@([^:/]+):(.+)$")
_SSH_SCHEME = re.compile(r"^ssh://git@([^/]+)/(.+)$")


def _normalize_remote_url(url: str) -> str:
    """Minimal normalization (roadmap open edge): unify `git@host:path` and
    `ssh://git@host/path` into the `https://host/path` shape, strip a
    trailing `.git` and trailing slash. Anything else compares literally."""

    text = url.strip()
    match = _SCP_STYLE.match(text) or _SSH_SCHEME.match(text)
    if match:
        text = f"https://{match.group(1)}/{match.group(2)}"
    if text.endswith(".git"):
        text = text[:-4]
    return text.rstrip("/")


def _classify_activity(entry: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    ahead = entry.get("ahead")
    behind = entry.get("behind")
    dirty = entry.get("dirty")
    reasons: dict[str, Any] = {}
    if ahead is not None:
        reasons["ahead"] = ahead
    if behind is not None:
        reasons["behind"] = behind
    if dirty is not None:
        reasons["dirty"] = dirty
    if (isinstance(ahead, int) and ahead > 0) or dirty is True:
        return "active_development", reasons
    if isinstance(behind, int) and behind > 0:
        return "behind_origin", reasons
    return "idle", reasons


def evaluate_workspace(
    workspace: DesiredWorkspace,
    node: DesiredNode | None,
    devices_by_id: dict[str, ActualDevice],
    *,
    now: datetime,
    stale_after_hours: int,
) -> WorkspaceEvaluation:
    realized_device_id = node.realized_device_id if node is not None else None
    device = devices_by_id.get(realized_device_id or "")
    gaps: list[dict[str, Any]] = []

    entry: dict[str, Any] | None = None
    if device is not None:
        observed = device.actual_facts().observed_workspaces
        if isinstance(observed, dict):
            entry = observed.get(workspace.slug)

    if entry is None:
        if workspace.desired_presence == "present":
            reason = "no_realized_device" if realized_device_id is None else "observation_missing"
            gaps.append({"code": "workspace_observation_missing", "reason": reason})
        # desired_presence == "absent" with no observation entry is the
        # common case for a retired workspace (Phase 1's probe hint only
        # covers active/present workspaces) -- nothing to report.
        return WorkspaceEvaluation(
            slug=workspace.slug,
            name=workspace.name,
            node_id=workspace.node_id,
            node_slug=workspace.node_slug,
            desired_presence=workspace.desired_presence,
            realized_device_id=realized_device_id,
            observation=None,
            gaps=gaps,
            activity_class=None,
            activity_reasons={},
        )

    checked_at = entry.get("checked_at")
    age = _age_hours(checked_at, now)
    if age is None:
        gaps.append({"code": "workspace_observation_missing", "reason": "checked_at_missing"})
    elif age > stale_after_hours:
        gaps.append({"code": "workspace_observation_stale", "age_hours": age})

    present = entry.get("present")
    activity_class: str | None = None
    activity_reasons: dict[str, Any] = {}

    if not isinstance(present, bool):
        gaps.append({"code": "workspace_observation_missing", "reason": "present_missing_or_invalid"})
    elif workspace.desired_presence == "present":
        if present is False:
            gaps.append({"code": "workspace_missing"})
        else:
            raw = entry.get("raw") if isinstance(entry.get("raw"), dict) else {}
            remote_url = entry.get("remote_url")
            if raw.get("is_git") is False or not remote_url:
                gaps.append({
                    "code": "workspace_identity_unknown",
                    "reason": "not_a_git_repository" if raw.get("is_git") is False else "remote_url_missing",
                })
            elif _normalize_remote_url(remote_url) != _normalize_remote_url(workspace.source_remote_url):
                gaps.append({
                    "code": "workspace_identity_mismatch",
                    "observed_remote_url": remote_url,
                    "declared_remote_url": workspace.source_remote_url,
                })
            activity_class, activity_reasons = _classify_activity(entry)
    else:
        if present is True:
            gaps.append({"code": "workspace_retired_present"})
            activity_class, activity_reasons = _classify_activity(entry)

    return WorkspaceEvaluation(
        slug=workspace.slug,
        name=workspace.name,
        node_id=workspace.node_id,
        node_slug=workspace.node_slug,
        desired_presence=workspace.desired_presence,
        realized_device_id=realized_device_id,
        observation=entry,
        gaps=gaps,
        activity_class=activity_class,
        activity_reasons=activity_reasons,
    )


def evaluate_all_workspaces(
    workspaces: list[DesiredWorkspace],
    nodes: list[DesiredNode],
    devices: list[ActualDevice],
    *,
    now: datetime,
    stale_after_hours: int = 24,
) -> dict[str, WorkspaceEvaluation]:
    nodes_by_id = {node.id: node for node in nodes}
    devices_by_id = {device.id: device for device in devices}
    return {
        workspace.id: evaluate_workspace(
            workspace,
            nodes_by_id.get(workspace.node_id),
            devices_by_id,
            now=now,
            stale_after_hours=stale_after_hours,
        )
        for workspace in workspaces
    }
