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

from nctl_core.config import Config, ConfigError
from nctl_core.nautobot import NautobotClient, NautobotError
from nctl_core.output import Envelope, EnvelopeError
from nctl_core.sources.braindump import (
    Attention,
    Authorship,
    BrainDumpRead,
    fetch_braindump_list,
    fetch_braindump_show,
)

BRAINDUMP_API_BASE = "/api/plugins/intent-catalog/braindumps"
ALIGNMENT_REVIEW_API_BASE = "/api/plugins/intent-catalog/alignment-reviews"

AUTHORSHIP_VALUES: tuple[str, ...] = ("user_direct", "agent_transcribed")

LIST_SCHEMA = "nctl.braindump.list.v1"
SHOW_SCHEMA = "nctl.braindump.show.v1"
CREATE_SCHEMA = "nctl.braindump.create.v1"
UPDATE_SCHEMA = "nctl.braindump.update.v1"
DELETE_SCHEMA = "nctl.braindump.delete.v1"
REVIEW_SCHEMA = "nctl.braindump.review.v1"
REVIEW_DELETE_SCHEMA = "nctl.braindump.review_delete.v1"


class BraindumpError(NautobotError):
    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None) -> None:
        self.code = code
        self.detail = detail or {}
        super().__init__(message)


class InvalidBraindumpIdError(BraindumpError):
    def __init__(self, value: str) -> None:
        super().__init__(
            "invalid_braindump_id", f"not a valid Braindump UUID: {value!r}", {"value": value}
        )


class InvalidAuthorshipError(BraindumpError):
    def __init__(self, value: str) -> None:
        super().__init__(
            "invalid_authorship",
            f"invalid authorship {value!r}; must be one of {', '.join(AUTHORSHIP_VALUES)}",
            {"value": value, "allowed": list(AUTHORSHIP_VALUES)},
        )


class InvalidTextError(BraindumpError):
    def __init__(self, field_name: str) -> None:
        super().__init__(
            "invalid_text",
            f"{field_name} must not be empty or whitespace-only",
            {"field": field_name},
        )


class InputConflictError(BraindumpError):
    def __init__(self, field_name: str, *, both: bool) -> None:
        reason = "both provided" if both else "neither provided"
        super().__init__(
            "input_conflict",
            f"exactly one of literal {field_name} or --file is required ({reason})",
            {"field": field_name},
        )


class NoUpdateFieldsError(BraindumpError):
    def __init__(self, braindump_id: str) -> None:
        super().__init__(
            "no_update_fields",
            "update requires at least one changed field (title, authorship, or body)",
            {"braindump_id": braindump_id},
        )


class InputFileError(BraindumpError):
    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(
            "input_file_error", f"cannot read {path}: {reason}", {"path": str(path)}
        )


class InputFileInvalidUtf8Error(BraindumpError):
    def __init__(self, path: Path) -> None:
        super().__init__(
            "input_file_invalid_utf8", f"{path} is not valid UTF-8", {"path": str(path)}
        )


class BraindumpNotFoundError(BraindumpError):
    def __init__(self, braindump_id: str) -> None:
        super().__init__(
            "braindump_not_found",
            f"no Braindump with id {braindump_id!r}",
            {"braindump_id": braindump_id},
        )


class BraindumpValidationFailedError(BraindumpError):
    def __init__(self, status_code: int, detail_text: str) -> None:
        super().__init__(
            "braindump_validation_failed",
            f"Braindump write rejected as invalid: HTTP {status_code}",
            {"status_code": status_code, "detail": detail_text[:200]},
        )


class BraindumpWriteRejectedError(BraindumpError):
    def __init__(self, status_code: int, detail_text: str) -> None:
        super().__init__(
            "braindump_write_rejected",
            f"Braindump write rejected: HTTP {status_code}",
            {"status_code": status_code, "detail": detail_text[:200]},
        )


class BraindumpConfirmationMismatchError(BraindumpError):
    def __init__(self, braindump_id: str) -> None:
        super().__init__(
            "braindump_confirmation_mismatch",
            f"GraphQL refetch of Braindump {braindump_id!r} did not match the requested write",
            {"braindump_id": braindump_id},
        )


class ReviewValidationFailedError(BraindumpError):
    def __init__(self, status_code: int, detail_text: str) -> None:
        super().__init__(
            "review_validation_failed",
            f"Alignment review write rejected as invalid: HTTP {status_code}",
            {"status_code": status_code, "detail": detail_text[:200]},
        )


class ReviewWriteRejectedError(BraindumpError):
    def __init__(self, status_code: int, detail_text: str) -> None:
        super().__init__(
            "review_write_rejected",
            f"Alignment review write rejected: HTTP {status_code}",
            {"status_code": status_code, "detail": detail_text[:200]},
        )


class ReviewConfirmationMismatchError(BraindumpError):
    def __init__(self, braindump_id: str) -> None:
        super().__init__(
            "review_confirmation_mismatch",
            f"GraphQL refetch of Braindump {braindump_id!r} did not show the requested review",
            {"braindump_id": braindump_id},
        )


class BraindumpDeleteRejectedError(BraindumpError):
    def __init__(self, status_code: int, detail_text: str) -> None:
        super().__init__(
            "braindump_delete_rejected",
            f"Braindump delete rejected: HTTP {status_code}",
            {"status_code": status_code, "detail": detail_text[:200]},
        )


class ReviewDeleteRejectedError(BraindumpError):
    def __init__(self, status_code: int, detail_text: str) -> None:
        super().__init__(
            "review_delete_rejected",
            f"Alignment review delete rejected: HTTP {status_code}",
            {"status_code": status_code, "detail": detail_text[:200]},
        )


class DeleteConfirmationMismatchError(BraindumpError):
    def __init__(self, target: str, target_id: str) -> None:
        super().__init__(
            "delete_confirmation_mismatch",
            f"GraphQL refetch still shows {target} {target_id!r} after delete",
            {"target": target, "target_id": target_id},
        )


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
        raise InvalidAuthorshipError(value)
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

    response = client.rest_post(
        f"{BRAINDUMP_API_BASE}/", {"title": title, "body": body, "authorship": authorship}
    )
    if not response.is_success:
        raise _write_error(response.status_code, response.text)

    new_id = response.json()["id"]
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

    response = client.rest_patch(f"{BRAINDUMP_API_BASE}/{canonical_id}/", payload)
    if not response.is_success:
        raise _write_error(response.status_code, response.text)

    confirmed = fetch_braindump_show(client, canonical_id)
    if confirmed is None or any(
        getattr(confirmed, field) != value for field, value in payload.items()
    ):
        raise BraindumpConfirmationMismatchError(canonical_id)

    return _to_record(confirmed), True


def _write_error(status_code: int, detail_text: str) -> BraindumpError:
    if status_code == 400:
        return BraindumpValidationFailedError(status_code, detail_text)
    return BraindumpWriteRejectedError(status_code, detail_text)


def _review_write_error(status_code: int, detail_text: str) -> BraindumpError:
    if status_code == 400:
        return ReviewValidationFailedError(status_code, detail_text)
    return ReviewWriteRejectedError(status_code, detail_text)


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
        response = client.rest_patch(
            f"{ALIGNMENT_REVIEW_API_BASE}/{existing_review.id}/", {"summary": summary}
        )
        if not response.is_success:
            raise _review_write_error(response.status_code, response.text)
    else:
        action = "created"
        response = client.rest_post(
            f"{ALIGNMENT_REVIEW_API_BASE}/", {"braindump": canonical_id, "summary": summary}
        )
        if response.status_code == 400:
            raced = fetch_braindump_show(client, canonical_id)
            raced_review = raced.alignment_review if raced is not None else None
            if raced_review is None:
                raise ReviewValidationFailedError(response.status_code, response.text)
            action = "replaced"
            response = client.rest_patch(
                f"{ALIGNMENT_REVIEW_API_BASE}/{raced_review.id}/", {"summary": summary}
            )
            if not response.is_success:
                raise _review_write_error(response.status_code, response.text)
        elif not response.is_success:
            raise _review_write_error(response.status_code, response.text)

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

    response = client.rest_delete(f"{BRAINDUMP_API_BASE}/{canonical_id}/")
    if not response.is_success:
        raise BraindumpDeleteRejectedError(response.status_code, response.text)

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

    response = client.rest_delete(f"{ALIGNMENT_REVIEW_API_BASE}/{review.id}/")
    if not response.is_success:
        raise ReviewDeleteRejectedError(response.status_code, response.text)

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
    cfg: Config, *, title: str, authorship: str, body: str
) -> Envelope[BraindumpCreateData]:
    client, token_error = _client_from_config(cfg)
    if client is None:
        return Envelope.build(CREATE_SCHEMA, BraindumpCreateData(), [token_error])  # type: ignore[list-item]

    try:
        record, changed = create_braindump(client, title=title, authorship=authorship, body=body)
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
) -> Envelope[BraindumpUpdateData]:
    client, token_error = _client_from_config(cfg)
    if client is None:
        return Envelope.build(UPDATE_SCHEMA, BraindumpUpdateData(), [token_error])  # type: ignore[list-item]

    try:
        record, changed = update_braindump(
            client, braindump_id, title=title, authorship=authorship, body=body
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


def build_braindump_review(cfg: Config, braindump_id: str, *, summary: str) -> Envelope[BraindumpReviewData]:
    client, token_error = _client_from_config(cfg)
    if client is None:
        return Envelope.build(REVIEW_SCHEMA, BraindumpReviewData(), [token_error])  # type: ignore[list-item]

    try:
        record, action = create_or_replace_review(client, braindump_id, summary=summary)
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
