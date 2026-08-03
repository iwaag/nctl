"""Core `nctl workflow-episode` operations.

Reads and writes both go through plain REST against
`/api/plugins/intent-catalog/workflow-episodes/` — unlike Braindump, there are no derived/joined
fields to justify a GraphQL read path, and the REST detail response already returns `raw_data`
verbatim (see `p2/plan.md` "Design hints"). Writes trust the 2xx response body rather than doing a
confirmation refetch (acceptable rigor for this experimental environment per plan.md).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from nctl_core.nautobot import NautobotClient
from nctl_core.workflow_episode_client import get_workflow_episode, list_workflow_episodes
from nctl_core.workflow_episode_errors import invalid_workflow_episode_id_error

STATUS_VALUES: tuple[str, ...] = ("candidate", "selected", "resolved", "dismissed")
DEFAULT_LIST_STATUSES: frozenset[str] = frozenset({"candidate", "selected"})


class WorkflowEpisodeRecord(BaseModel):
    id: str
    title: str
    status: str
    raw_data: dict = {}
    created: datetime
    last_updated: datetime


class WorkflowEpisodeListItem(BaseModel):
    id: str
    title: str
    status: str
    created: datetime
    last_updated: datetime


class WorkflowEpisodeListData(BaseModel):
    items: list[WorkflowEpisodeListItem] = []
    count: int = 0


class WorkflowEpisodeShowData(BaseModel):
    episode: WorkflowEpisodeRecord | None = None


def validate_workflow_episode_id(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise invalid_workflow_episode_id_error(value) from exc


def list_episodes(client: NautobotClient, *, statuses: frozenset[str] | None) -> list[WorkflowEpisodeListItem]:
    """`statuses=None` returns every episode regardless of status; an empty set matches nothing."""
    rows = list_workflow_episodes(client)
    if statuses is not None:
        rows = [row for row in rows if row["status"] in statuses]
    return [_to_list_item(row) for row in rows]


def show_episode(client: NautobotClient, episode_id: str) -> WorkflowEpisodeRecord:
    canonical_id = validate_workflow_episode_id(episode_id)
    row = get_workflow_episode(client, canonical_id)
    return _to_record(row)


def _to_record(row: dict) -> WorkflowEpisodeRecord:
    return WorkflowEpisodeRecord(
        id=row["id"],
        title=row["title"],
        status=row["status"],
        raw_data=row.get("raw_data") or {},
        created=row["created"],
        last_updated=row["last_updated"],
    )


def _to_list_item(row: dict) -> WorkflowEpisodeListItem:
    return WorkflowEpisodeListItem(
        id=row["id"],
        title=row["title"],
        status=row["status"],
        created=row["created"],
        last_updated=row["last_updated"],
    )
