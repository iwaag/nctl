"""REST transport for WorkflowEpisode reads and writes."""

from __future__ import annotations

from typing import Any

from nctl_core.nautobot import NautobotClient
from nctl_core.workflow_episode_errors import workflow_episode_not_found_error

WORKFLOW_EPISODE_API_BASE = "/api/plugins/intent-catalog/workflow-episodes"


def list_workflow_episodes(client: NautobotClient) -> list[dict[str, Any]]:
    response = client.rest_get(f"{WORKFLOW_EPISODE_API_BASE}/")
    response.raise_for_status()
    return response.json()["results"]


def get_workflow_episode(client: NautobotClient, episode_id: str) -> dict[str, Any]:
    response = client.rest_get(f"{WORKFLOW_EPISODE_API_BASE}/{episode_id}/")
    if response.status_code == 404:
        raise workflow_episode_not_found_error(episode_id)
    response.raise_for_status()
    return response.json()
