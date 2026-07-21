"""Typed GraphQL reader for the Braindump/Alignment Review exchange diary (Phase 2 Step 2.2).

Reads only; REST writes belong to `nctl_core.braindump` (Step 2.3+). This reader is deliberately
separate from `sources/snapshot.py`: Braindump/AlignmentReview are conversational context above the
deterministic desired/actual/drift domain (see `devdocs/big/braindump/roadmap.md`) and must not be
imported into drift comparators, reconcile, or production composition.

`authorship` is serialized by Nautobot GraphQL as the enum *name* (`USER_DIRECT`,
`AGENT_TRANSCRIBED`); lowercasing it produces exactly the domain vocabulary
(`user_direct`/`agent_transcribed`), same convention as `sources/desired.py`. `body` and `summary`
are passed through untouched -- this reader never parses, trims, or otherwise interprets them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from nctl_core.nautobot import NautobotClient

LIST_QUERY = """
query ListBrainDumps {
  braindump_documents {
    id
    title
    body
    authorship
    created
    last_updated
    alignment_review {
      id
      summary
      created
      last_updated
    }
  }
}
"""

SHOW_QUERY = """
query ShowBrainDump($id: ID!) {
  braindump_document(id: $id) {
    id
    title
    body
    authorship
    created
    last_updated
    alignment_review {
      id
      summary
      created
      last_updated
    }
  }
}
"""

Authorship = Literal["user_direct", "agent_transcribed"]
Attention = Literal["unreviewed", "needs_attention", "review_present"]


class AlignmentReviewRead(BaseModel):
    id: str
    summary: str
    created: datetime
    last_updated: datetime


class BrainDumpRead(BaseModel):
    id: str
    title: str
    body: str
    authorship: Authorship
    created: datetime
    last_updated: datetime
    alignment_review: AlignmentReviewRead | None = None

    @property
    def attention(self) -> Attention:
        review = self.alignment_review
        return compute_attention(
            self.last_updated, review.last_updated if review is not None else None
        )


def compute_attention(
    braindump_last_updated: datetime, review_last_updated: datetime | None
) -> Attention:
    """The three-state freshness hint from roadmap.md's "Freshness" section and plan.md Decision 4."""
    if review_last_updated is None:
        return "unreviewed"
    if review_last_updated < braindump_last_updated:
        return "needs_attention"
    return "review_present"


def fetch_braindump_list(client: NautobotClient) -> list[BrainDumpRead]:
    data = client.graphql(LIST_QUERY)
    records = [_build_braindump(row) for row in data["braindump_documents"]]
    # Stable multi-key sort: apply ascending tie-breakers first, then the descending primary key.
    records.sort(key=lambda r: r.id)
    records.sort(key=lambda r: r.title)
    records.sort(key=lambda r: r.last_updated, reverse=True)
    return records


def fetch_braindump_show(client: NautobotClient, braindump_id: str) -> BrainDumpRead | None:
    data = client.graphql(SHOW_QUERY, {"id": braindump_id})
    row = data["braindump_document"]
    if row is None:
        return None
    return _build_braindump(row)


def _build_braindump(row: dict[str, Any]) -> BrainDumpRead:
    review = row.get("alignment_review")
    return BrainDumpRead(
        id=row["id"],
        title=row["title"],
        body=row["body"],
        authorship=row["authorship"].lower(),
        created=row["created"],
        last_updated=row["last_updated"],
        alignment_review=_build_review(review),
    )


def _build_review(row: dict[str, Any] | None) -> AlignmentReviewRead | None:
    if row is None:
        return None
    return AlignmentReviewRead(
        id=row["id"],
        summary=row["summary"],
        created=row["created"],
        last_updated=row["last_updated"],
    )
