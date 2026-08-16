"""Pure desired-agent versus observed-agent evaluation (agent_intent p1 step 5).

Deliberately the same shape as `workspace_evaluation.py`, and for the same
reason: convergent verdicts leave as `gaps` (the `{"code": ..., **evidence}`
shape `comparators.py` turns into `DiffRecord`s), while the informational
`liveness_class` travels in its own field so no comparator can accidentally
promote it to drift.

**Registration is drift. Liveness is not.**

- A gap means the cluster is not arranged the way it was declared: the agent's
  Zulip account is missing or deactivated, a channel it must hear is not
  subscribed, or its Plane membership is absent. Each is deterministic,
  actionable, and false-positive-free — the collector reports presence, not
  guesses.
- `liveness_class` (`polling` / `stale` / `unobserved`) is the freshness of the
  agent's own status file, which is a property of a process that may be
  restarting, mid-deploy, or deliberately stopped. p1 refuses to call that
  drift. This mirrors `activity_class` exactly, and the discipline is the
  point: promote staleness to a gap only once there is real false-positive
  data saying it deserves to be one.

Liveness is read from the agent's `DesiredWorkspace` observation
(`agent_status`, put there by nodeutils), so an agent with no declared
workspace is `unobserved` rather than dead — the difference between "we looked
and saw nothing" and "we never looked" is kept visible.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from nctl_core.sources.actual import ActualDevice, ObservedAgentRegistration
from nctl_core.sources.desired import DesiredAgent, DesiredNode, DesiredWorkspace

LIVENESS_CLASSES = ("polling", "stale", "unobserved")

# The status file is rewritten after every successful poll, and pyagag's
# long-poll ceiling is 90s, so three windows of silence is the first moment
# staleness is more likely than an unlucky gap between polls.
DEFAULT_POLL_INTERVAL_SECONDS = 90
STALE_AFTER_POLL_INTERVALS = 3


@dataclass(frozen=True)
class AgentEvaluation:
    slug: str
    name: str
    lifecycle: str
    workspace_slug: str | None
    observation: ObservedAgentRegistration | None
    gaps: list[dict[str, Any]]
    liveness_class: str
    liveness_reasons: dict[str, Any]


def _observation_age_seconds(value: Any, now: datetime) -> float | None:
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (now.astimezone(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds())


def _registration_gaps(
    agent: DesiredAgent, observation: ObservedAgentRegistration | None
) -> list[dict[str, Any]]:
    if observation is None:
        return [{"code": "agent_registration_unobserved"}]

    gaps: list[dict[str, Any]] = []
    if agent.zulip_user_id is None:
        gaps.append({"code": "agent_zulip_identity_undeclared"})
    elif not observation.zulip_present:
        gaps.append({"code": "agent_zulip_account_missing", "zulip_user_id": agent.zulip_user_id})
    elif not observation.zulip_is_active:
        gaps.append({"code": "agent_zulip_account_deactivated", "zulip_user_id": agent.zulip_user_id})

    if observation.zulip_present:
        missing = sorted(set(agent.desired_zulip_channels) - set(observation.zulip_channels))
        if missing:
            gaps.append({"code": "agent_zulip_channel_unsubscribed", "channels": missing})

    if not agent.plane_user_id:
        gaps.append({"code": "agent_plane_identity_undeclared"})
    elif not observation.plane_present:
        gaps.append({"code": "agent_plane_membership_missing", "plane_user_id": agent.plane_user_id})
    return gaps


def _liveness(
    agent: DesiredAgent,
    workspace: DesiredWorkspace | None,
    devices_by_id: dict[str, ActualDevice],
    nodes_by_id: dict[str, DesiredNode],
    *,
    now: datetime,
    stale_after_seconds: float,
) -> tuple[str, dict[str, Any]]:
    if workspace is None:
        return "unobserved", {"reason": "no_desired_workspace"}
    node = nodes_by_id.get(workspace.node_id)
    device = devices_by_id.get((node.realized_device_id if node else None) or "")
    if device is None:
        return "unobserved", {"reason": "no_realized_device", "workspace": workspace.slug}
    observed = device.actual_facts().observed_workspaces
    entry = observed.get(workspace.slug) if isinstance(observed, dict) else None
    if not isinstance(entry, dict):
        return "unobserved", {"reason": "workspace_unobserved", "workspace": workspace.slug}
    status = entry.get("agent_status")
    if not isinstance(status, dict) or not status.get("readable"):
        reason = "status_file_unreadable" if isinstance(status, dict) else "no_status_file"
        return "unobserved", {"reason": reason, "workspace": workspace.slug}

    # The node computed `age_seconds` on its own clock at collection time; add
    # the age of the collection itself, measured here. Neither term compares
    # two different machines' clocks, so skew never enters the answer.
    age = status.get("age_seconds")
    if not isinstance(age, (int, float)):
        return "unobserved", {"reason": "status_age_missing", "workspace": workspace.slug}
    collection_age = _observation_age_seconds(status.get("checked_at"), now)
    total = float(age) + (collection_age or 0.0)
    reasons: dict[str, Any] = {
        "workspace": workspace.slug,
        "age_seconds_at_collection": round(float(age), 3),
        "seconds_since_collection": None if collection_age is None else round(collection_age, 3),
        "effective_age_seconds": round(total, 3),
    }
    if status.get("last_error"):
        reasons["last_error"] = status["last_error"]
    return ("polling" if total <= stale_after_seconds else "stale"), reasons


def evaluate_agent(
    agent: DesiredAgent,
    workspace: DesiredWorkspace | None,
    registrations_by_slug: dict[str, ObservedAgentRegistration],
    devices_by_id: dict[str, ActualDevice],
    nodes_by_id: dict[str, DesiredNode],
    *,
    now: datetime,
    stale_after_seconds: float,
) -> AgentEvaluation:
    observation = registrations_by_slug.get(agent.slug)
    gaps = _registration_gaps(agent, observation) if agent.lifecycle == "active" else []
    liveness_class, liveness_reasons = _liveness(
        agent, workspace, devices_by_id, nodes_by_id,
        now=now, stale_after_seconds=stale_after_seconds,
    )
    return AgentEvaluation(
        slug=agent.slug,
        name=agent.name,
        lifecycle=agent.lifecycle,
        workspace_slug=workspace.slug if workspace is not None else None,
        observation=observation,
        gaps=gaps,
        liveness_class=liveness_class,
        liveness_reasons=liveness_reasons,
    )


def evaluate_all_agents(
    agents: list[DesiredAgent],
    workspaces: list[DesiredWorkspace],
    nodes: list[DesiredNode],
    devices: list[ActualDevice],
    registrations: list[ObservedAgentRegistration],
    *,
    now: datetime,
    stale_after_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS * STALE_AFTER_POLL_INTERVALS,
) -> dict[str, AgentEvaluation]:
    workspaces_by_id = {workspace.id: workspace for workspace in workspaces}
    nodes_by_id = {node.id: node for node in nodes}
    devices_by_id = {device.id: device for device in devices}
    registrations_by_slug = {row.agent_slug: row for row in registrations if row.agent_slug}
    return {
        agent.id: evaluate_agent(
            agent,
            workspaces_by_id.get(agent.workspace_id or ""),
            registrations_by_slug,
            devices_by_id,
            nodes_by_id,
            now=now,
            stale_after_seconds=stale_after_seconds,
        )
        for agent in agents
    }
