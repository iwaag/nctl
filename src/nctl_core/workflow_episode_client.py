"""REST transport for WorkflowEpisode reads and writes."""

from __future__ import annotations

from typing import Any

from nctl_core.nautobot import NautobotClient
from nctl_core.workflow_episode_errors import (
    workflow_episode_create_rejected_error,
    workflow_episode_not_found_error,
    workflow_episode_transition_ineligible_error,
    workflow_episode_transition_rejected_error,
    workflow_episode_write_rejected_error,
)

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


def create_workflow_episode(client: NautobotClient, payload: dict[str, Any]) -> dict[str, Any]:
    response = client.rest_post(f"{WORKFLOW_EPISODE_API_BASE}/", payload)
    if not response.is_success:
        raise workflow_episode_create_rejected_error(response.status_code, response.text)
    return response.json()


def write_workflow_episode_namespace(
    client: NautobotClient, episode_id: str, namespace: str, payload: dict[str, Any]
) -> dict[str, Any]:
    response = client.rest_post(f"{WORKFLOW_EPISODE_API_BASE}/{episode_id}/{namespace}/", payload)
    if response.status_code == 404:
        raise workflow_episode_not_found_error(episode_id)
    if not response.is_success:
        raise workflow_episode_write_rejected_error(response.status_code, response.text)
    return response.json()


def transition_workflow_episode(
    client: NautobotClient, episode_id: str, action: str, *, new_status: str
) -> dict[str, Any]:
    response = client.rest_post(f"{WORKFLOW_EPISODE_API_BASE}/{episode_id}/{action}/", {})
    if response.status_code == 404:
        raise workflow_episode_not_found_error(episode_id)
    if response.status_code == 409:
        raise workflow_episode_transition_ineligible_error(episode_id, new_status, response.text)
    if not response.is_success:
        raise workflow_episode_transition_rejected_error(response.status_code, response.text)
    return response.json()
