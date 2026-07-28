"""Braindump envelope construction and human presentation."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, TypeVar

from nctl_core.braindump import (
    BraindumpCreateData, BraindumpDeleteData, BraindumpListData, BraindumpReviewData,
    BraindumpReviewDeleteData, BraindumpShowData, BraindumpUpdateData, create_braindump,
    create_or_replace_review, delete_braindump, delete_review, list_braindumps,
    resolve_text_input, show_braindump, update_braindump,
)
from nctl_core.braindump_errors import BraindumpError
from nctl_core.config import Config, ConfigError
from nctl_core.nautobot import NautobotClient, NautobotError
from nctl_core.output import Envelope, EnvelopeError

LIST_SCHEMA = "nctl.braindump.list.v1"
SHOW_SCHEMA = "nctl.braindump.show.v1"
CREATE_SCHEMA = "nctl.braindump.create.v1"
UPDATE_SCHEMA = "nctl.braindump.update.v1"
DELETE_SCHEMA = "nctl.braindump.delete.v1"
REVIEW_SCHEMA = "nctl.braindump.review.v1"
REVIEW_DELETE_SCHEMA = "nctl.braindump.review_delete.v1"
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
    except BraindumpError as exc:
        return Envelope.build(schema, empty, [EnvelopeError(code=exc.code, message=str(exc), detail=exc.detail)])
    except NautobotError as exc:
        return Envelope.build(schema, empty, [EnvelopeError(code="nautobot_connection_error", message=str(exc))])
    finally:
        client.close()


def build_braindump_list(cfg: Config) -> Envelope[BraindumpListData]:
    def action(c: NautobotClient) -> BraindumpListData:
        items = list_braindumps(c)
        return BraindumpListData(items=items, count=len(items))
    return _build(cfg, LIST_SCHEMA, BraindumpListData(), action)


def build_braindump_show(cfg: Config, braindump_id: str) -> Envelope[BraindumpShowData]:
    return _build(cfg, SHOW_SCHEMA, BraindumpShowData(), lambda c: BraindumpShowData(braindump=show_braindump(c, braindump_id)))


def build_braindump_create(cfg: Config, *, title: str, authorship: str, body: str | None = None, body_file: Path | None = None) -> Envelope[BraindumpCreateData]:
    def action(c: NautobotClient) -> BraindumpCreateData:
        record, changed = create_braindump(c, title=title, authorship=authorship, body=resolve_text_input(field_name="body", literal=body, file=body_file))
        return BraindumpCreateData(braindump=record, changed=changed)
    return _build(cfg, CREATE_SCHEMA, BraindumpCreateData(), action)


def build_braindump_update(cfg: Config, braindump_id: str, *, title: str | None = None, authorship: str | None = None, body: str | None = None, body_file: Path | None = None) -> Envelope[BraindumpUpdateData]:
    def action(c: NautobotClient) -> BraindumpUpdateData:
        resolved = resolve_text_input(field_name="body", literal=body, file=body_file) if body is not None or body_file is not None else None
        record, changed = update_braindump(c, braindump_id, title=title, authorship=authorship, body=resolved)
        return BraindumpUpdateData(braindump=record, changed=changed)
    return _build(cfg, UPDATE_SCHEMA, BraindumpUpdateData(), action)


def build_braindump_delete(cfg: Config, braindump_id: str) -> Envelope[BraindumpDeleteData]:
    def action(c: NautobotClient) -> BraindumpDeleteData:
        title, deleted, review_deleted = delete_braindump(c, braindump_id)
        return BraindumpDeleteData(id=braindump_id, title=title, deleted=deleted, review_deleted=review_deleted)
    return _build(cfg, DELETE_SCHEMA, BraindumpDeleteData(), action)


def build_braindump_review(cfg: Config, braindump_id: str, *, summary: str | None = None, summary_file: Path | None = None) -> Envelope[BraindumpReviewData]:
    def action(c: NautobotClient) -> BraindumpReviewData:
        record, verb = create_or_replace_review(c, braindump_id, summary=resolve_text_input(field_name="summary", literal=summary, file=summary_file))
        return BraindumpReviewData(braindump=record, action=verb)
    return _build(cfg, REVIEW_SCHEMA, BraindumpReviewData(), action)


def build_braindump_review_delete(cfg: Config, braindump_id: str) -> Envelope[BraindumpReviewDeleteData]:
    def action(c: NautobotClient) -> BraindumpReviewDeleteData:
        deleted, review_id = delete_review(c, braindump_id)
        return BraindumpReviewDeleteData(braindump=show_braindump(c, braindump_id), deleted=deleted, review_id=review_id)
    return _build(cfg, REVIEW_DELETE_SCHEMA, BraindumpReviewDeleteData(), action)


def _errors(envelope: Envelope) -> str:
    return "\n".join(f"error[{error.code}]: {error.message}" for error in envelope.errors)


def render_braindump_list_text(e):
    if not e.ok: return _errors(e)
    return "\n".join([f"braindumps: {e.data.count}"] + [f"  {x.id}  {x.title!r:<30} {x.authorship:<17} updated {x.last_updated}  {'Unreviewed' if not x.review_present else f'review updated {x.review_last_updated}'}  [{x.attention}]" for x in e.data.items])


def render_braindump_show_text(e):
    if not e.ok: return _errors(e)
    x = e.data.braindump
    if x is None: return "no Braindump"
    lines = ["User-originated Braindump", f"  id: {x.id}", f"  title: {x.title}", f"  authorship: {x.authorship}", f"  created: {x.created}", f"  last_updated: {x.last_updated}", f"  attention: {x.attention}", "  body:", x.body, "", "AI Alignment Review"]
    if x.alignment_review is None: lines.append("  Unreviewed")
    else:
        r=x.alignment_review; lines += [f"  id: {r.id}", f"  created: {r.created}", f"  last_updated: {r.last_updated}", "  summary:", r.summary]
    return "\n".join(lines)

def render_braindump_create_text(e):
    if not e.ok: return _errors(e)
    x=e.data.braindump; return f"created braindump {x.id} ({x.title!r}, {x.authorship}) at {x.last_updated}"
def render_braindump_update_text(e):
    if not e.ok: return _errors(e)
    x=e.data.braindump; return f"updated braindump {x.id} ({x.title!r}) at {x.last_updated}" if e.data.changed else f"braindump {x.id} ({x.title!r}): no change (already up to date)"
def render_braindump_delete_text(e):
    if not e.ok: return _errors(e)
    x=e.data; return f"deleted braindump {x.id} ({x.title!r}): {'review also deleted' if x.review_deleted else 'no review was present'}"
def render_braindump_review_text(e):
    if not e.ok: return _errors(e)
    x=e.data; return f"{x.action} review for braindump {x.braindump.id} ({x.braindump.title!r}) at {x.braindump.alignment_review.last_updated}"
def render_braindump_review_delete_text(e):
    if not e.ok: return _errors(e)
    x=e.data; bid=x.braindump.id if x.braindump else "?"; return f"deleted review {x.review_id} for braindump {bid}; braindump is now Unreviewed" if x.deleted else f"braindump {bid}: no review present (no change)"
