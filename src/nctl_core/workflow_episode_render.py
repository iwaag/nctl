"""WorkflowEpisode envelope construction and human presentation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, TypeVar

from nctl_core.config import Config, ConfigError
from nctl_core.nautobot import NautobotClient, NautobotError
from nctl_core.output import Envelope, EnvelopeError
from nctl_core.workflow_episode import (
    DEFAULT_LIST_STATUSES,
    WorkflowEpisodeCreateData,
    WorkflowEpisodeListData,
    WorkflowEpisodeShowData,
    WorkflowEpisodeTransitionData,
    WorkflowEpisodeWriteData,
    create_episode,
    dismiss_episode,
    list_episodes,
    resolve_json_object_input,
    resolve_optional_json_object_input,
    resolve_episode,
    select_episode,
    show_episode,
    write_namespace,
)
from nctl_core.workflow_episode_errors import WorkflowEpisodeError

LIST_SCHEMA = "nctl.workflow_episode.list.v1"
SHOW_SCHEMA = "nctl.workflow_episode.show.v1"
CREATE_SCHEMA = "nctl.workflow_episode.create.v1"
WRITE_SCHEMA = "nctl.workflow_episode.write.v1"
SELECT_SCHEMA = "nctl.workflow_episode.select.v1"
RESOLVE_SCHEMA = "nctl.workflow_episode.resolve.v1"
DISMISS_SCHEMA = "nctl.workflow_episode.dismiss.v1"
T = TypeVar("T")


def _client_from_config(cfg: Config) -> tuple[NautobotClient | None, EnvelopeError | None]:
    try:
        return NautobotClient(cfg.nautobot.url, cfg.nautobot.resolve_token()), None
    except ConfigError as exc:
        return None, EnvelopeError(code="nautobot_token_error", message=str(exc))


def _build(cfg: Config, schema: str, empty: T, action: Callable[[NautobotClient], T]) -> Envelope[T]:
    client, token_error = _client_from_config(cfg)
    if client is None:
        return Envelope.build(schema, empty, [token_error])  # type: ignore[list-item]
    try:
        return Envelope.build(schema, action(client))
    except WorkflowEpisodeError as exc:
        return Envelope.build(schema, empty, [EnvelopeError(code=exc.code, message=str(exc), detail=exc.detail)])
    except NautobotError as exc:
        return Envelope.build(schema, empty, [EnvelopeError(code="nautobot_connection_error", message=str(exc))])
    finally:
        client.close()


def build_workflow_episode_list(cfg: Config, *, statuses: frozenset[str] | None = DEFAULT_LIST_STATUSES) -> Envelope[WorkflowEpisodeListData]:
    def action(c: NautobotClient) -> WorkflowEpisodeListData:
        items = list_episodes(c, statuses=statuses)
        return WorkflowEpisodeListData(items=items, count=len(items))
    return _build(cfg, LIST_SCHEMA, WorkflowEpisodeListData(), action)


def build_workflow_episode_show(cfg: Config, episode_id: str) -> Envelope[WorkflowEpisodeShowData]:
    return _build(
        cfg, SHOW_SCHEMA, WorkflowEpisodeShowData(),
        lambda c: WorkflowEpisodeShowData(episode=show_episode(c, episode_id)),
    )


def build_workflow_episode_create(
    cfg: Config, *, title: str, raw_data: str | None = None, raw_data_file: Path | None = None
) -> Envelope[WorkflowEpisodeCreateData]:
    def action(c: NautobotClient) -> WorkflowEpisodeCreateData:
        resolved = resolve_optional_json_object_input(field_name="raw_data", literal=raw_data, file=raw_data_file)
        record, changed = create_episode(c, title=title, raw_data=resolved)
        return WorkflowEpisodeCreateData(episode=record, changed=changed)
    return _build(cfg, CREATE_SCHEMA, WorkflowEpisodeCreateData(), action)


def build_workflow_episode_write(
    cfg: Config, episode_id: str, namespace: str, *, data: str | None = None, data_file: Path | None = None
) -> Envelope[WorkflowEpisodeWriteData]:
    def action(c: NautobotClient) -> WorkflowEpisodeWriteData:
        payload = resolve_json_object_input(field_name="data", literal=data, file=data_file)
        record = write_namespace(c, episode_id, namespace, payload)
        return WorkflowEpisodeWriteData(episode=record, namespace=namespace, changed=True)
    return _build(cfg, WRITE_SCHEMA, WorkflowEpisodeWriteData(), action)


def build_workflow_episode_select(cfg: Config, episode_id: str) -> Envelope[WorkflowEpisodeTransitionData]:
    def action(c: NautobotClient) -> WorkflowEpisodeTransitionData:
        record, changed = select_episode(c, episode_id)
        return WorkflowEpisodeTransitionData(episode=record, changed=changed)
    return _build(cfg, SELECT_SCHEMA, WorkflowEpisodeTransitionData(), action)


def build_workflow_episode_resolve(cfg: Config, episode_id: str) -> Envelope[WorkflowEpisodeTransitionData]:
    def action(c: NautobotClient) -> WorkflowEpisodeTransitionData:
        record, changed = resolve_episode(c, episode_id)
        return WorkflowEpisodeTransitionData(episode=record, changed=changed)
    return _build(cfg, RESOLVE_SCHEMA, WorkflowEpisodeTransitionData(), action)


def build_workflow_episode_dismiss(cfg: Config, episode_id: str) -> Envelope[WorkflowEpisodeTransitionData]:
    def action(c: NautobotClient) -> WorkflowEpisodeTransitionData:
        record, changed = dismiss_episode(c, episode_id)
        return WorkflowEpisodeTransitionData(episode=record, changed=changed)
    return _build(cfg, DISMISS_SCHEMA, WorkflowEpisodeTransitionData(), action)


def _errors(envelope: Envelope) -> str:
    return "\n".join(f"error[{error.code}]: {error.message}" for error in envelope.errors)


def render_workflow_episode_list_text(e: Envelope[WorkflowEpisodeListData]) -> str:
    if not e.ok:
        return _errors(e)
    lines = [f"workflow episodes: {e.data.count}"]
    for x in e.data.items:
        lines.append(f"  {x.id}  {x.title!r:<40} {x.status:<10} updated {x.last_updated}")
    return "\n".join(lines)


def _render_namespace_section(title: str, value: dict) -> list[str]:
    lines = [f"{title}:"]
    if not value:
        lines.append("  (not yet recorded)")
    else:
        lines.append("  " + json.dumps(value, indent=2, sort_keys=True).replace("\n", "\n  "))
    return lines


def render_workflow_episode_show_text(e: Envelope[WorkflowEpisodeShowData]) -> str:
    if not e.ok:
        return _errors(e)
    x = e.data.episode
    if x is None:
        return "no WorkflowEpisode"
    lines = [
        "WorkflowEpisode",
        f"  id: {x.id}",
        f"  title: {x.title}",
        f"  status: {x.status}",
        f"  created: {x.created}",
        f"  last_updated: {x.last_updated}",
        "",
    ]
    for namespace in ("report", "assessment", "references", "resolution"):
        lines += _render_namespace_section(namespace, x.raw_data.get(namespace) or {})
        lines.append("")
    return "\n".join(lines).rstrip("\n")


def render_workflow_episode_create_text(e: Envelope[WorkflowEpisodeCreateData]) -> str:
    if not e.ok:
        return _errors(e)
    x = e.data.episode
    return f"created workflow episode {x.id} ({x.title!r}) status={x.status} at {x.last_updated}"


def render_workflow_episode_write_text(e: Envelope[WorkflowEpisodeWriteData]) -> str:
    if not e.ok:
        return _errors(e)
    x = e.data
    return f"wrote {x.namespace} for workflow episode {x.episode.id} ({x.episode.title!r}); status={x.episode.status}"


def render_workflow_episode_transition_text(e: Envelope[WorkflowEpisodeTransitionData]) -> str:
    if not e.ok:
        return _errors(e)
    x = e.data.episode
    return f"workflow episode {x.id} ({x.title!r}) is now {x.status}"
