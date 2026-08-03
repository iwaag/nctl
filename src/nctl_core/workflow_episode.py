"""Core `nctl workflow-episode` operations.

Reads and writes both go through plain REST against
`/api/plugins/intent-catalog/workflow-episodes/` — unlike Braindump, there are no derived/joined
fields to justify a GraphQL read path, and the REST detail response already returns `raw_data`
verbatim (see `p2/plan.md` "Design hints"). Writes trust the 2xx response body rather than doing a
confirmation refetch (acceptable rigor for this experimental environment per plan.md).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel

from nctl_core.nautobot import NautobotClient
from nctl_core.workflow_episode_client import (
    create_workflow_episode as post_create_workflow_episode,
    get_workflow_episode,
    list_workflow_episodes,
    write_workflow_episode_namespace as post_write_workflow_episode_namespace,
)
from nctl_core.workflow_episode_errors import (
    invalid_json_error,
    invalid_namespace_payload_error,
    invalid_text_error,
    invalid_workflow_episode_id_error,
    input_conflict_error,
    input_file_error,
    input_file_invalid_utf8_error,
)

STATUS_VALUES: tuple[str, ...] = ("candidate", "selected", "resolved", "dismissed")
NAMESPACE_VALUES: tuple[str, ...] = ("report", "assessment", "references", "resolution")
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


class WorkflowEpisodeCreateData(BaseModel):
    episode: WorkflowEpisodeRecord | None = None
    changed: bool = False


class WorkflowEpisodeWriteData(BaseModel):
    episode: WorkflowEpisodeRecord | None = None
    namespace: str = ""
    changed: bool = False


def validate_workflow_episode_id(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise invalid_workflow_episode_id_error(value) from exc


def _require_nonblank(field_name: str, value: str) -> str:
    if not value.strip():
        raise invalid_text_error(field_name)
    return value


def _read_json_object_text(field_name: str, text: str) -> dict:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise invalid_json_error(field_name, str(exc)) from exc
    if not isinstance(value, dict):
        raise invalid_namespace_payload_error(field_name)
    return value


def _read_file_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="strict")
    except UnicodeDecodeError:
        raise input_file_invalid_utf8_error(str(path)) from None
    except OSError as exc:
        raise input_file_error(str(path), str(exc)) from exc


def resolve_json_object_input(*, field_name: str, literal: str | None, file: Path | None) -> dict:
    """Resolve exactly one JSON-object source (literal string vs `--file`)."""
    if literal is not None and file is not None:
        raise input_conflict_error(field_name, both=True)
    if literal is None and file is None:
        raise input_conflict_error(field_name, both=False)
    text = _read_file_text(file) if file is not None else literal
    return _read_json_object_text(field_name, text)  # type: ignore[arg-type]


def resolve_optional_json_object_input(*, field_name: str, literal: str | None, file: Path | None) -> dict | None:
    """Like `resolve_json_object_input`, but `neither` means "omit" rather than a conflict error."""
    if literal is None and file is None:
        return None
    return resolve_json_object_input(field_name=field_name, literal=literal, file=file)


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


# -- write operations ---------------------------------------------------------------------------


def create_episode(client: NautobotClient, *, title: str, raw_data: dict | None) -> tuple[WorkflowEpisodeRecord, bool]:
    title = _require_nonblank("title", title)
    payload: dict = {"title": title}
    if raw_data is not None:
        payload["raw_data"] = raw_data
    row = post_create_workflow_episode(client, payload)
    return _to_record(row), True


def write_namespace(client: NautobotClient, episode_id: str, namespace: str, payload: dict) -> WorkflowEpisodeRecord:
    canonical_id = validate_workflow_episode_id(episode_id)
    row = post_write_workflow_episode_namespace(client, canonical_id, namespace, payload)
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
