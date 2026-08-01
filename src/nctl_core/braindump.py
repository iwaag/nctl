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
from uuid import UUID

from pydantic import BaseModel

from nctl_core.braindump_client import (
    complete_braindump as post_complete_braindump,
    create_braindump as post_braindump,
    purge_braindump as purge_braindump_request,
    supersede_braindumps as post_supersede_braindumps,
    create_review,
    delete_review as delete_review_request,
    update_review,
)
from nctl_core.braindump_errors import (
    braindump_confirmation_mismatch_error, braindump_not_found_error,
    complete_confirmation_mismatch_error,
    delete_confirmation_mismatch_error, input_conflict_error, input_file_error,
    input_file_invalid_utf8_error, invalid_authorship_error, invalid_braindump_id_error,
    invalid_supersede_old_ids_error,
    invalid_text_error, review_confirmation_mismatch_error,
    review_validation_failed_error,
    supersede_confirmation_mismatch_error,
)
from nctl_core.nautobot import NautobotClient
from nctl_core.sources.braindump import (
    Attention,
    Authorship,
    BraindumpStatus,
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
    status: BraindumpStatus
    completion_reason: str = ""
    created: datetime
    last_updated: datetime
    review_present: bool
    attention: Attention
    alignment_review: AlignmentReviewRecord | None = None


class BrainDumpListItem(BaseModel):
    id: str
    title: str
    authorship: Authorship
    status: BraindumpStatus
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


class BraindumpSupersedeData(BaseModel):
    braindump: BrainDumpRecord | None = None
    superseded_ids: list[str] = []
    changed: bool = False


class BraindumpCompleteData(BaseModel):
    braindump: BrainDumpRecord | None = None
    changed: bool = False


class BraindumpReviewData(BaseModel):
    braindump: BrainDumpRecord | None = None
    action: str = ""


class BraindumpReviewDeleteData(BaseModel):
    braindump: BrainDumpRecord | None = None
    deleted: bool = False
    review_id: str | None = None


class BraindumpPurgeData(BaseModel):
    braindump: BrainDumpRecord | None = None
    outcome: str = ""
    alignment_review_present: bool = False


# -- input resolution/validation ----------------------------------------------------------------


def resolve_text_input(*, field_name: str, literal: str | None, file: Path | None) -> str:
    """Resolve exactly one prose source and validate it is non-blank.

    File content is read with `errors="strict"` UTF-8 decoding and is never stripped, reflowed, or
    otherwise transformed; only `str.strip()` is used to *decide* emptiness (plan.md Decision 2).
    """
    if literal is not None and file is not None:
        raise input_conflict_error(field_name, both=True)
    if literal is None and file is None:
        raise input_conflict_error(field_name, both=False)

    if file is not None:
        try:
            text = file.read_text(encoding="utf-8", errors="strict")
        except UnicodeDecodeError:
            raise input_file_invalid_utf8_error(file) from None
        except OSError as exc:
            raise input_file_error(file, str(exc)) from exc
    else:
        text = literal  # type: ignore[assignment]

    if not text.strip():
        raise invalid_text_error(field_name)
    return text


def validate_authorship(value: str) -> Authorship:
    if value not in AUTHORSHIP_VALUES:
        raise invalid_authorship_error(value, AUTHORSHIP_VALUES)
    return value  # type: ignore[return-value]


def validate_braindump_id(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise invalid_braindump_id_error(value) from exc


def _require_nonblank(field_name: str, value: str) -> str:
    if not value.strip():
        raise invalid_text_error(field_name)
    return value


# -- read operations --------------------------------------------------------------------------


def list_braindumps(client: NautobotClient, *, include_superseded: bool = False) -> list[BrainDumpListItem]:
    """`include_superseded` also includes `completed` rows: both are reference-only history."""
    records = fetch_braindump_list(client)
    if not include_superseded:
        records = [record for record in records if record.status == "active"]
    return [_to_list_item(record) for record in records]


def show_braindump(client: NautobotClient, braindump_id: str) -> BrainDumpRecord:
    canonical_id = validate_braindump_id(braindump_id)
    record = fetch_braindump_show(client, canonical_id)
    if record is None:
        raise braindump_not_found_error(canonical_id)
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
        raise braindump_confirmation_mismatch_error(new_id)

    return _to_record(confirmed), True


def supersede_braindumps(
    client: NautobotClient, *, old_ids: list[str], title: str, authorship: str, body: str
) -> tuple[BrainDumpRecord, list[str], bool]:
    if not old_ids:
        raise invalid_supersede_old_ids_error("at least one --old Braindump UUID is required")
    canonical_old_ids = [validate_braindump_id(value) for value in old_ids]
    if len(set(canonical_old_ids)) != len(canonical_old_ids):
        raise invalid_supersede_old_ids_error("each --old Braindump UUID must be supplied once")
    title = _require_nonblank("title", title)
    body = _require_nonblank("body", body)
    authorship = validate_authorship(authorship)
    new_id, superseded_ids = post_supersede_braindumps(
        client, {"old_ids": canonical_old_ids, "title": title, "body": body, "authorship": authorship}
    )
    if superseded_ids != canonical_old_ids:
        raise supersede_confirmation_mismatch_error(new_id)
    replacement = fetch_braindump_show(client, new_id)
    old_records = [fetch_braindump_show(client, old_id) for old_id in canonical_old_ids]
    if (
        replacement is None
        or replacement.title != title
        or replacement.body != body
        or replacement.authorship != authorship
        or replacement.status != "active"
        or any(record is None or record.status != "superseded" for record in old_records)
    ):
        raise supersede_confirmation_mismatch_error(new_id)
    return _to_record(replacement), canonical_old_ids, True


def complete_braindump(client: NautobotClient, braindump_id: str, *, reason: str) -> tuple[BrainDumpRecord, bool]:
    """Directly transition one active Braindump to completed; no replacement row is created."""
    canonical_id = validate_braindump_id(braindump_id)
    reason = _require_nonblank("reason", reason)

    post_complete_braindump(client, canonical_id, reason=reason)
    confirmed = fetch_braindump_show(client, canonical_id)
    if (
        confirmed is None
        or confirmed.status != "completed"
        or confirmed.completion_reason != reason
    ):
        raise complete_confirmation_mismatch_error(canonical_id)

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
        raise braindump_not_found_error(canonical_id)

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
                raise review_validation_failed_error(response.status_code, response.text)
            action = "replaced"
            update_review(client, raced_review.id, summary)

    confirmed = fetch_braindump_show(client, canonical_id)
    if (
        confirmed is None
        or confirmed.alignment_review is None
        or confirmed.alignment_review.summary != summary
    ):
        raise review_confirmation_mismatch_error(canonical_id)

    return _to_record(confirmed), action


def delete_review(client: NautobotClient, braindump_id: str) -> tuple[bool, str | None]:
    """Delete only the current review for a Braindump, leaving it unreviewed.

    An absent review is an idempotent no-op: returns `(False, None)`, not an error.
    """
    canonical_id = validate_braindump_id(braindump_id)
    current = fetch_braindump_show(client, canonical_id)
    if current is None:
        raise braindump_not_found_error(canonical_id)

    review = current.alignment_review
    if review is None:
        return False, None

    delete_review_request(client, review.id)

    confirmed = fetch_braindump_show(client, canonical_id)
    if confirmed is None or confirmed.alignment_review is not None:
        raise delete_confirmation_mismatch_error("review", review.id)

    return True, review.id


def purge_braindump(client: NautobotClient, braindump_id: str, *, apply: bool) -> BraindumpPurgeData:
    """Plan or execute a dedicated purge of one superseded Braindump."""
    canonical_id = validate_braindump_id(braindump_id)
    result = purge_braindump_request(client, canonical_id, apply=apply)
    if result["outcome"] == "already_purged":
        return BraindumpPurgeData(outcome="already_purged")
    review_present = result["alignment_review_present"]
    record = BrainDumpRecord.model_validate(
        {
            **result["braindump"],
            "review_present": review_present,
            "attention": "review_present" if review_present else "unreviewed",
            "alignment_review": None,
        }
    )
    return BraindumpPurgeData(
        braindump=record,
        outcome=result["outcome"],
        alignment_review_present=review_present,
    )


# -- record projection ---------------------------------------------------------------------------


def _to_record(read: BrainDumpRead) -> BrainDumpRecord:
    review = read.alignment_review
    return BrainDumpRecord(
        id=read.id,
        title=read.title,
        body=read.body,
        authorship=read.authorship,
        status=read.status,
        completion_reason=read.completion_reason,
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
        status=read.status,
        created=read.created,
        last_updated=read.last_updated,
        review_present=review is not None,
        review_id=review.id if review is not None else None,
        review_last_updated=review.last_updated if review is not None else None,
        attention=read.attention,
    )
