"""Core `nctl braindump` operations (Phase 2 Steps 2.3-2.4, plan.md Decisions 2, 5-8).

Reads go through `nctl_core.sources.braindump` (GraphQL); writes go through REST at the two
Phase 1 collections (`/api/plugins/intent-catalog/braindumps/`,
`/api/plugins/intent-catalog/alignment-reviews/`). Every successful write is confirmed by a fresh
GraphQL refetch before an envelope reports `changed=True`; a mismatch raises a confirmation error
rather than fabricating success (same convention as `nctl_core.lifecycle`).

`body`/`summary` are opaque strings end to end: accepted exactly as given (a literal CLI argument,
or a UTF-8 file read with `errors="strict"`), never trimmed, reformatted, or interpreted before
being sent to REST.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from nctl_core.braindump_client import (
    create_braindump as post_braindump,
    create_review,
    delete_braindump as delete_braindump_request,
    delete_review as delete_review_request,
    update_braindump as patch_braindump,
    update_review,
)
from nctl_core.braindump_errors import *  # error vocabulary belongs to braindump_errors
from nctl_core.nautobot import NautobotClient
from nctl_core.sources.braindump import (
    Attention,
    Authorship,
    BrainDumpRead,
    fetch_braindump_list,
    fetch_braindump_show,
)

AUTHORSHIP_VALUES: tuple[str, ...] = ("user_direct", "agent_transcribed")


# -- typed output record shapes (plan.md Decision 5) ------------------------------------------


class AlignmentReviewRecord(BaseModel):
    id: str
    summary: str
    created: datetime
    last_updated: datetime


class BrainDumpRecord(BaseModel):
    id: str
    title: str
    body: str
    authorship: Authorship
    created: datetime
    last_updated: datetime
    review_present: bool
    attention: Attention
    alignment_review: AlignmentReviewRecord | None = None


class BrainDumpListItem(BaseModel):
    id: str
    title: str
    authorship: Authorship
    created: datetime
    last_updated: datetime
    review_present: bool
    review_id: str | None = None
    review_last_updated: datetime | None = None
    attention: Attention


class BraindumpListData(BaseModel):
    items: list[BrainDumpListItem] = []
    count: int = 0


class BraindumpShowData(BaseModel):
    braindump: BrainDumpRecord | None = None


class BraindumpCreateData(BaseModel):
    braindump: BrainDumpRecord | None = None
    changed: bool = False


class BraindumpUpdateData(BaseModel):
    braindump: BrainDumpRecord | None = None
    changed: bool = False


class BraindumpDeleteData(BaseModel):
    id: str = ""
    title: str = ""
    deleted: bool = False
    review_deleted: bool = False


class BraindumpReviewData(BaseModel):
    braindump: BrainDumpRecord | None = None
    action: str = ""


class BraindumpReviewDeleteData(BaseModel):
    braindump: BrainDumpRecord | None = None
    deleted: bool = False
    review_id: str | None = None


# -- input resolution/validation ----------------------------------------------------------------


def resolve_text_input(*, field_name: str, literal: str | None, file: Path | None) -> str:
    """Resolve exactly one prose source and validate it is non-blank.

    File content is read with `errors="strict"` UTF-8 decoding and is never stripped, reflowed, or
    otherwise transformed; only `str.strip()` is used to *decide* emptiness (plan.md Decision 2).
    """
    if literal is not None and file is not None:
        raise InputConflictError(field_name, both=True)
    if literal is None and file is None:
        raise InputConflictError(field_name, both=False)

    if file is not None:
        try:
            text = file.read_text(encoding="utf-8", errors="strict")
        except UnicodeDecodeError:
            raise InputFileInvalidUtf8Error(file) from None
        except OSError as exc:
            raise InputFileError(file, str(exc)) from exc
    else:
        text = literal  # type: ignore[assignment]

    if not text.strip():
        raise InvalidTextError(field_name)
    return text


def validate_authorship(value: str) -> Authorship:
    if value not in AUTHORSHIP_VALUES:
        raise InvalidAuthorshipError(value, AUTHORSHIP_VALUES)
    return value  # type: ignore[return-value]


def validate_braindump_id(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvalidBraindumpIdError(value) from exc


def _require_nonblank(field_name: str, value: str) -> str:
    if not value.strip():
        raise InvalidTextError(field_name)
    return value


# -- read operations --------------------------------------------------------------------------


def list_braindumps(client: NautobotClient) -> list[BrainDumpListItem]:
    return [_to_list_item(record) for record in fetch_braindump_list(client)]


def show_braindump(client: NautobotClient, braindump_id: str) -> BrainDumpRecord:
    canonical_id = validate_braindump_id(braindump_id)
    record = fetch_braindump_show(client, canonical_id)
    if record is None:
        raise BraindumpNotFoundError(canonical_id)
    return _to_record(record)


# -- write operations ---------------------------------------------------------------------------


def create_braindump(
    client: NautobotClient, *, title: str, authorship: str, body: str
) -> tuple[BrainDumpRecord, bool]:
    title = _require_nonblank("title", title)
    body = _require_nonblank("body", body)
    authorship = validate_authorship(authorship)

    new_id = post_braindump(client, {"title": title, "body": body, "authorship": authorship})
    confirmed = fetch_braindump_show(client, new_id)
    if (
        confirmed is None
        or confirmed.title != title
        or confirmed.body != body
        or confirmed.authorship != authorship
    ):
        raise BraindumpConfirmationMismatchError(new_id)

    return _to_record(confirmed), True


def update_braindump(
    client: NautobotClient,
    braindump_id: str,
    *,
    title: str | None = None,
    authorship: str | None = None,
    body: str | None = None,
) -> tuple[BrainDumpRecord, bool]:
    canonical_id = validate_braindump_id(braindump_id)
    if title is None and authorship is None and body is None:
        raise NoUpdateFieldsError(canonical_id)

    payload: dict[str, Any] = {}
    if title is not None:
        payload["title"] = _require_nonblank("title", title)
    if authorship is not None:
        payload["authorship"] = validate_authorship(authorship)
    if body is not None:
        payload["body"] = _require_nonblank("body", body)

    current = fetch_braindump_show(client, canonical_id)
    if current is None:
        raise BraindumpNotFoundError(canonical_id)

    if all(getattr(current, field) == value for field, value in payload.items()):
        return _to_record(current), False

    patch_braindump(client, canonical_id, payload)

    confirmed = fetch_braindump_show(client, canonical_id)
    if confirmed is None or any(
        getattr(confirmed, field) != value for field, value in payload.items()
    ):
        raise BraindumpConfirmationMismatchError(canonical_id)

    return _to_record(confirmed), True


def create_or_replace_review(
    client: NautobotClient, braindump_id: str, *, summary: str
) -> tuple[BrainDumpRecord, str]:
    """Create-or-replace the one current review for a Braindump (plan.md Decision 6).

    Returns `(record, action)` where `action` is `"created"` or `"replaced"`. A POST that fails with
    a validation error is retried as a PATCH exactly once, and only when a refetch shows another
    writer won a uniqueness race in the interim (a review now exists that did not before); any other
    400 is a genuine validation failure and is raised unchanged.
    """
    canonical_id = validate_braindump_id(braindump_id)
    summary = _require_nonblank("summary", summary)

    current = fetch_braindump_show(client, canonical_id)
    if current is None:
        raise BraindumpNotFoundError(canonical_id)

    existing_review = current.alignment_review
    if existing_review is not None:
        action = "replaced"
        update_review(client, existing_review.id, summary)
    else:
        action = "created"
        response = create_review(client, canonical_id, summary)
        if response.status_code == 400:
            raced = fetch_braindump_show(client, canonical_id)
            raced_review = raced.alignment_review if raced is not None else None
            if raced_review is None:
                raise ReviewValidationFailedError(response.status_code, response.text)
            action = "replaced"
            update_review(client, raced_review.id, summary)

    confirmed = fetch_braindump_show(client, canonical_id)
    if (
        confirmed is None
        or confirmed.alignment_review is None
        or confirmed.alignment_review.summary != summary
    ):
        raise ReviewConfirmationMismatchError(canonical_id)

    return _to_record(confirmed), action


def delete_braindump(client: NautobotClient, braindump_id: str) -> tuple[str, bool, bool]:
    """Delete a Braindump (and its review, via DB cascade). Returns `(title, deleted, review_deleted)`."""
    canonical_id = validate_braindump_id(braindump_id)
    current = fetch_braindump_show(client, canonical_id)
    if current is None:
        raise BraindumpNotFoundError(canonical_id)

    title = current.title
    review_existed = current.alignment_review is not None

    delete_braindump_request(client, canonical_id)

    confirmed = fetch_braindump_show(client, canonical_id)
    if confirmed is not None:
        raise DeleteConfirmationMismatchError("braindump", canonical_id)

    return title, True, review_existed


def delete_review(client: NautobotClient, braindump_id: str) -> tuple[bool, str | None]:
    """Delete only the current review for a Braindump, leaving it unreviewed.

    An absent review is an idempotent no-op: returns `(False, None)`, not an error.
    """
    canonical_id = validate_braindump_id(braindump_id)
    current = fetch_braindump_show(client, canonical_id)
    if current is None:
        raise BraindumpNotFoundError(canonical_id)

    review = current.alignment_review
    if review is None:
        return False, None

    delete_review_request(client, review.id)

    confirmed = fetch_braindump_show(client, canonical_id)
    if confirmed is None or confirmed.alignment_review is not None:
        raise DeleteConfirmationMismatchError("review", review.id)

    return True, review.id


# -- record projection ---------------------------------------------------------------------------


def _to_record(read: BrainDumpRead) -> BrainDumpRecord:
    review = read.alignment_review
    return BrainDumpRecord(
        id=read.id,
        title=read.title,
        body=read.body,
        authorship=read.authorship,
        created=read.created,
        last_updated=read.last_updated,
        review_present=review is not None,
        attention=read.attention,
        alignment_review=(
            AlignmentReviewRecord(
                id=review.id,
                summary=review.summary,
                created=review.created,
                last_updated=review.last_updated,
            )
            if review is not None
            else None
        ),
    )


def _to_list_item(read: BrainDumpRead) -> BrainDumpListItem:
    review = read.alignment_review
    return BrainDumpListItem(
        id=read.id,
        title=read.title,
        authorship=read.authorship,
        created=read.created,
        last_updated=read.last_updated,
        review_present=review is not None,
        review_id=review.id if review is not None else None,
        review_last_updated=review.last_updated if review is not None else None,
        attention=read.attention,
    )


# -- CLI-facing envelope builders (never raise) --------------------------------------------------


def _client_from_config(cfg: Config) -> tuple[NautobotClient | None, EnvelopeError | None]:
    try:
        token = cfg.nautobot.resolve_token()
    except ConfigError as exc:
        return None, EnvelopeError(code="nautobot_token_error", message=str(exc))
    return NautobotClient(cfg.nautobot.url, token), None


def build_braindump_list(cfg: Config) -> Envelope[BraindumpListData]:
    client, token_error = _client_from_config(cfg)
    if client is None:
        return Envelope.build(LIST_SCHEMA, BraindumpListData(), [token_error])  # type: ignore[list-item]

    try:
        items = list_braindumps(client)
    except BraindumpError as exc:
        return Envelope.build(
            LIST_SCHEMA, BraindumpListData(), [EnvelopeError(code=exc.code, message=str(exc), detail=exc.detail)]
        )
    except NautobotError as exc:
        return Envelope.build(
            LIST_SCHEMA, BraindumpListData(), [EnvelopeError(code="nautobot_connection_error", message=str(exc))]
        )
    finally:
        client.close()

    return Envelope.build(LIST_SCHEMA, BraindumpListData(items=items, count=len(items)))


def build_braindump_show(cfg: Config, braindump_id: str) -> Envelope[BraindumpShowData]:
    client, token_error = _client_from_config(cfg)
    if client is None:
        return Envelope.build(SHOW_SCHEMA, BraindumpShowData(), [token_error])  # type: ignore[list-item]

    try:
        record = show_braindump(client, braindump_id)
    except BraindumpError as exc:
        return Envelope.build(
            SHOW_SCHEMA, BraindumpShowData(), [EnvelopeError(code=exc.code, message=str(exc), detail=exc.detail)]
        )
    except NautobotError as exc:
        return Envelope.build(
            SHOW_SCHEMA, BraindumpShowData(), [EnvelopeError(code="nautobot_connection_error", message=str(exc))]
        )
    finally:
        client.close()

    return Envelope.build(SHOW_SCHEMA, BraindumpShowData(braindump=record))


def build_braindump_create(
    cfg: Config,
    *,
    title: str,
    authorship: str,
    body: str | None = None,
    body_file: Path | None = None,
) -> Envelope[BraindumpCreateData]:
    client, token_error = _client_from_config(cfg)
    if client is None:
        return Envelope.build(CREATE_SCHEMA, BraindumpCreateData(), [token_error])  # type: ignore[list-item]

    try:
        resolved_body = resolve_text_input(field_name="body", literal=body, file=body_file)
        record, changed = create_braindump(
            client, title=title, authorship=authorship, body=resolved_body
        )
    except BraindumpError as exc:
        return Envelope.build(
            CREATE_SCHEMA, BraindumpCreateData(), [EnvelopeError(code=exc.code, message=str(exc), detail=exc.detail)]
        )
    except NautobotError as exc:
        return Envelope.build(
            CREATE_SCHEMA, BraindumpCreateData(), [EnvelopeError(code="nautobot_connection_error", message=str(exc))]
        )
    finally:
        client.close()

    return Envelope.build(CREATE_SCHEMA, BraindumpCreateData(braindump=record, changed=changed))


def build_braindump_update(
    cfg: Config,
    braindump_id: str,
    *,
    title: str | None = None,
    authorship: str | None = None,
    body: str | None = None,
    body_file: Path | None = None,
) -> Envelope[BraindumpUpdateData]:
    client, token_error = _client_from_config(cfg)
    if client is None:
        return Envelope.build(UPDATE_SCHEMA, BraindumpUpdateData(), [token_error])  # type: ignore[list-item]

    try:
        resolved_body = (
            resolve_text_input(field_name="body", literal=body, file=body_file)
            if body is not None or body_file is not None
            else None
        )
        record, changed = update_braindump(
            client, braindump_id, title=title, authorship=authorship, body=resolved_body
        )
    except BraindumpError as exc:
        return Envelope.build(
            UPDATE_SCHEMA, BraindumpUpdateData(), [EnvelopeError(code=exc.code, message=str(exc), detail=exc.detail)]
        )
    except NautobotError as exc:
        return Envelope.build(
            UPDATE_SCHEMA, BraindumpUpdateData(), [EnvelopeError(code="nautobot_connection_error", message=str(exc))]
        )
    finally:
        client.close()

    return Envelope.build(UPDATE_SCHEMA, BraindumpUpdateData(braindump=record, changed=changed))


def build_braindump_delete(cfg: Config, braindump_id: str) -> Envelope[BraindumpDeleteData]:
    client, token_error = _client_from_config(cfg)
    if client is None:
        return Envelope.build(DELETE_SCHEMA, BraindumpDeleteData(), [token_error])  # type: ignore[list-item]

    try:
        title, deleted, review_deleted = delete_braindump(client, braindump_id)
    except BraindumpError as exc:
        return Envelope.build(
            DELETE_SCHEMA, BraindumpDeleteData(), [EnvelopeError(code=exc.code, message=str(exc), detail=exc.detail)]
        )
    except NautobotError as exc:
        return Envelope.build(
            DELETE_SCHEMA, BraindumpDeleteData(), [EnvelopeError(code="nautobot_connection_error", message=str(exc))]
        )
    finally:
        client.close()

    return Envelope.build(
        DELETE_SCHEMA,
        BraindumpDeleteData(id=braindump_id, title=title, deleted=deleted, review_deleted=review_deleted),
    )


def build_braindump_review(
    cfg: Config,
    braindump_id: str,
    *,
    summary: str | None = None,
    summary_file: Path | None = None,
) -> Envelope[BraindumpReviewData]:
    client, token_error = _client_from_config(cfg)
    if client is None:
        return Envelope.build(REVIEW_SCHEMA, BraindumpReviewData(), [token_error])  # type: ignore[list-item]

    try:
        resolved_summary = resolve_text_input(field_name="summary", literal=summary, file=summary_file)
        record, action = create_or_replace_review(client, braindump_id, summary=resolved_summary)
    except BraindumpError as exc:
        return Envelope.build(
            REVIEW_SCHEMA, BraindumpReviewData(), [EnvelopeError(code=exc.code, message=str(exc), detail=exc.detail)]
        )
    except NautobotError as exc:
        return Envelope.build(
            REVIEW_SCHEMA, BraindumpReviewData(), [EnvelopeError(code="nautobot_connection_error", message=str(exc))]
        )
    finally:
        client.close()

    return Envelope.build(REVIEW_SCHEMA, BraindumpReviewData(braindump=record, action=action))


def build_braindump_review_delete(cfg: Config, braindump_id: str) -> Envelope[BraindumpReviewDeleteData]:
    client, token_error = _client_from_config(cfg)
    if client is None:
        return Envelope.build(REVIEW_DELETE_SCHEMA, BraindumpReviewDeleteData(), [token_error])  # type: ignore[list-item]

    try:
        deleted, review_id = delete_review(client, braindump_id)
        record = show_braindump(client, braindump_id)
    except BraindumpError as exc:
        return Envelope.build(
            REVIEW_DELETE_SCHEMA,
            BraindumpReviewDeleteData(),
            [EnvelopeError(code=exc.code, message=str(exc), detail=exc.detail)],
        )
    except NautobotError as exc:
        return Envelope.build(
            REVIEW_DELETE_SCHEMA,
            BraindumpReviewDeleteData(),
            [EnvelopeError(code="nautobot_connection_error", message=str(exc))],
        )
    finally:
        client.close()

    return Envelope.build(
        REVIEW_DELETE_SCHEMA,
        BraindumpReviewDeleteData(braindump=record, deleted=deleted, review_id=review_id),
    )


# -- human renderers ------------------------------------------------------------------------------


def _error_lines(envelope: Envelope) -> list[str]:
    return [f"error[{error.code}]: {error.message}" for error in envelope.errors]


def render_braindump_list_text(envelope: Envelope[BraindumpListData]) -> str:
    if not envelope.ok:
        return "\n".join(_error_lines(envelope))
    data = envelope.data
    lines = [f"braindumps: {data.count}"]
    for item in data.items:
        review = "Unreviewed" if not item.review_present else f"review updated {item.review_last_updated}"
        lines.append(
            f"  {item.id}  {item.title!r:<30} {item.authorship:<17} updated {item.last_updated}"
            f"  {review}  [{item.attention}]"
        )
    return "\n".join(lines)


def render_braindump_show_text(envelope: Envelope[BraindumpShowData]) -> str:
    if not envelope.ok:
        return "\n".join(_error_lines(envelope))
    braindump = envelope.data.braindump
    if braindump is None:
        return "no Braindump"

    lines = [
        "User-originated Braindump",
        f"  id: {braindump.id}",
        f"  title: {braindump.title}",
        f"  authorship: {braindump.authorship}",
        f"  created: {braindump.created}",
        f"  last_updated: {braindump.last_updated}",
        f"  attention: {braindump.attention}",
        "  body:",
        braindump.body,
        "",
        "AI Alignment Review",
    ]
    review = braindump.alignment_review
    if review is None:
        lines.append("  Unreviewed")
    else:
        lines += [
            f"  id: {review.id}",
            f"  created: {review.created}",
            f"  last_updated: {review.last_updated}",
            "  summary:",
            review.summary,
        ]
    return "\n".join(lines)


def render_braindump_create_text(envelope: Envelope[BraindumpCreateData]) -> str:
    if not envelope.ok:
        return "\n".join(_error_lines(envelope))
    record = envelope.data.braindump
    return f"created braindump {record.id} ({record.title!r}, {record.authorship}) at {record.last_updated}"


def render_braindump_update_text(envelope: Envelope[BraindumpUpdateData]) -> str:
    if not envelope.ok:
        return "\n".join(_error_lines(envelope))
    data = envelope.data
    record = data.braindump
    if not data.changed:
        return f"braindump {record.id} ({record.title!r}): no change (already up to date)"
    return f"updated braindump {record.id} ({record.title!r}) at {record.last_updated}"


def render_braindump_delete_text(envelope: Envelope[BraindumpDeleteData]) -> str:
    if not envelope.ok:
        return "\n".join(_error_lines(envelope))
    data = envelope.data
    cascade = "review also deleted" if data.review_deleted else "no review was present"
    return f"deleted braindump {data.id} ({data.title!r}): {cascade}"


def render_braindump_review_text(envelope: Envelope[BraindumpReviewData]) -> str:
    if not envelope.ok:
        return "\n".join(_error_lines(envelope))
    data = envelope.data
    record = data.braindump
    review = record.alignment_review
    return f"{data.action} review for braindump {record.id} ({record.title!r}) at {review.last_updated}"


def render_braindump_review_delete_text(envelope: Envelope[BraindumpReviewDeleteData]) -> str:
    if not envelope.ok:
        return "\n".join(_error_lines(envelope))
    data = envelope.data
    braindump_id = data.braindump.id if data.braindump is not None else "?"
    if not data.deleted:
        return f"braindump {braindump_id}: no review present (no change)"
    return f"deleted review {data.review_id} for braindump {braindump_id}; braindump is now Unreviewed"
