"""Braindump envelope construction and human presentation."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, TypeVar

from nctl_core.braindump import (
    BraindumpCreateData, BraindumpListData, BraindumpPurgeData, BraindumpReviewData, BraindumpReviewDeleteData,
    BraindumpShowData, BraindumpSupersedeData, create_braindump, create_or_replace_review, delete_review,
    list_braindumps, purge_braindump, resolve_text_input, show_braindump,
    supersede_braindumps,
)
from nctl_core.braindump_errors import BraindumpError
from nctl_core.config import Config, ConfigError
from nctl_core.nautobot import NautobotClient, NautobotError
from nctl_core.output import Envelope, EnvelopeError

LIST_SCHEMA = "nctl.braindump.list.v1"
SHOW_SCHEMA = "nctl.braindump.show.v1"
CREATE_SCHEMA = "nctl.braindump.create.v1"
SUPERSEDE_SCHEMA = "nctl.braindump.supersede.v1"
REVIEW_SCHEMA = "nctl.braindump.review.v1"
REVIEW_DELETE_SCHEMA = "nctl.braindump.review_delete.v1"
PURGE_SCHEMA = "nctl.braindump.purge.v1"
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


def build_braindump_list(cfg: Config, *, include_superseded: bool = False) -> Envelope[BraindumpListData]:
    def action(c: NautobotClient) -> BraindumpListData:
        items = list_braindumps(c, include_superseded=include_superseded)
        return BraindumpListData(items=items, count=len(items))
    return _build(cfg, LIST_SCHEMA, BraindumpListData(), action)


def build_braindump_show(cfg: Config, braindump_id: str) -> Envelope[BraindumpShowData]:
    return _build(cfg, SHOW_SCHEMA, BraindumpShowData(), lambda c: BraindumpShowData(braindump=show_braindump(c, braindump_id)))


def build_braindump_create(cfg: Config, *, title: str, authorship: str, body: str | None = None, body_file: Path | None = None) -> Envelope[BraindumpCreateData]:
    def action(c: NautobotClient) -> BraindumpCreateData:
        record, changed = create_braindump(c, title=title, authorship=authorship, body=resolve_text_input(field_name="body", literal=body, file=body_file))
        return BraindumpCreateData(braindump=record, changed=changed)
    return _build(cfg, CREATE_SCHEMA, BraindumpCreateData(), action)


def build_braindump_supersede(cfg: Config, *, old_ids: list[str], title: str, authorship: str, body: str | None = None, body_file: Path | None = None) -> Envelope[BraindumpSupersedeData]:
    def action(c: NautobotClient) -> BraindumpSupersedeData:
        record, superseded_ids, changed = supersede_braindumps(
            c, old_ids=old_ids, title=title, authorship=authorship,
            body=resolve_text_input(field_name="body", literal=body, file=body_file),
        )
        return BraindumpSupersedeData(braindump=record, superseded_ids=superseded_ids, changed=changed)
    return _build(cfg, SUPERSEDE_SCHEMA, BraindumpSupersedeData(), action)


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


def build_braindump_purge(cfg: Config, braindump_id: str, *, apply: bool = False) -> Envelope[BraindumpPurgeData]:
    return _build(
        cfg, PURGE_SCHEMA, BraindumpPurgeData(),
        lambda c: purge_braindump(c, braindump_id, apply=apply),
    )


def _errors(envelope: Envelope) -> str:
    return "\n".join(f"error[{error.code}]: {error.message}" for error in envelope.errors)


def render_braindump_list_text(e):
    if not e.ok: return _errors(e)
    return "\n".join([f"braindumps: {e.data.count}"] + [f"  {x.id}  {x.title!r:<30} {x.authorship:<17} {x.status:<11} updated {x.last_updated}  {'Unreviewed' if not x.review_present else f'review updated {x.review_last_updated}'}  [{x.attention}]" for x in e.data.items])


def render_braindump_show_text(e):
    if not e.ok: return _errors(e)
    x = e.data.braindump
    if x is None: return "no Braindump"
    lines = ["User-originated Braindump", f"  id: {x.id}", f"  title: {x.title}", f"  authorship: {x.authorship}", f"  status: {x.status}", f"  created: {x.created}", f"  last_updated: {x.last_updated}", f"  attention: {x.attention}", "  body:", x.body, "", "AI Alignment Review"]
    if x.alignment_review is None: lines.append("  Unreviewed")
    else:
        r=x.alignment_review; lines += [f"  id: {r.id}", f"  created: {r.created}", f"  last_updated: {r.last_updated}", "  summary:", r.summary]
    return "\n".join(lines)

def render_braindump_create_text(e):
    if not e.ok: return _errors(e)
    x=e.data.braindump; return f"created braindump {x.id} ({x.title!r}, {x.authorship}) at {x.last_updated}"
def render_braindump_supersede_text(e):
    if not e.ok: return _errors(e)
    x=e.data; return f"created active replacement {x.braindump.id} and superseded: {', '.join(x.superseded_ids)}"
def render_braindump_review_text(e):
    if not e.ok: return _errors(e)
    x=e.data; return f"{x.action} review for braindump {x.braindump.id} ({x.braindump.title!r}) at {x.braindump.alignment_review.last_updated}"
def render_braindump_review_delete_text(e):
    if not e.ok: return _errors(e)
    x=e.data; bid=x.braindump.id if x.braindump else "?"; return f"deleted review {x.review_id} for braindump {bid}; braindump is now Unreviewed" if x.deleted else f"braindump {bid}: no review present (no change)"
def render_braindump_purge_text(e):
    if not e.ok: return _errors(e)
    x=e.data
    if x.outcome == "already_purged": return "braindump already purged (no change)"
    review = "with Alignment Review" if x.alignment_review_present else "without Alignment Review"
    verb = "purged" if x.outcome == "purged" else "purge plan"
    return f"{verb}: {x.braindump.id} ({x.braindump.title!r}, {review})"
