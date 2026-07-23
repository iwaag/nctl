"""`nctl session new TASK_NAME` -- create an isolated agent-workspace session folder.

Agent-facing task manuals (e.g. `agentdocs/brainforge/README.md`) tell an agent to invent a
`<session-slug>` and create `.local/workspace/<task_name>/<slug>/` as scratch space isolated per
session. Leaving slug generation to each agent produced inconsistent results (different agents/
models picked different formats, some too short to avoid collisions). This centralizes it: one
deterministic-enough scheme (date + optional topic + a short random suffix), collision-checked by
actually creating the directory.

This command only creates the empty session directory -- subfolders like `sources/`, `reviews/`,
`evidence/` stay the caller's responsibility to create lazily, per brainforge's README.
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from nctl_core.config import Config
from nctl_core.output import Envelope, EnvelopeError

SESSION_SCHEMA = "nctl.session.new.v1"

TASK_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
MAX_SLUG_ATTEMPTS = 5
RANDOM_SUFFIX_LEN = 4  # hex chars


class SessionError(Exception):
    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None) -> None:
        self.code = code
        self.detail = detail or {}
        super().__init__(message)


class InvalidTaskNameError(SessionError):
    def __init__(self, task_name: str) -> None:
        super().__init__(
            "invalid_task_name",
            f"invalid task_name {task_name!r}; must match {TASK_NAME_RE.pattern}",
            {"task_name": task_name},
        )


class SessionCreateFailedError(SessionError):
    def __init__(self, workspace_dir: Path, attempts: int) -> None:
        super().__init__(
            "session_create_failed",
            f"could not create a unique session folder under {workspace_dir} after {attempts} attempts",
            {"workspace_dir": str(workspace_dir), "attempts": attempts},
        )


class SessionNewData(BaseModel):
    task_name: str
    topic: str | None
    slug: str
    path: str


def _slugify_topic(topic: str) -> str:
    lowered = topic.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    return slug


def create_session(
    repo_root: Path, task_name: str, topic: str | None = None, *, now: datetime | None = None
) -> SessionNewData:
    """Pure operation: validate task_name, then create a fresh, collision-free session dir.

    Raises a `SessionError` subclass on failure; never returns a partial result.
    """

    if not TASK_NAME_RE.match(task_name):
        raise InvalidTaskNameError(task_name)

    topic_slug = _slugify_topic(topic) if topic else None
    date_part = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    workspace_dir = repo_root / ".local" / "workspace" / task_name

    for _ in range(MAX_SLUG_ATTEMPTS):
        suffix = secrets.token_hex(RANDOM_SUFFIX_LEN // 2)
        parts = [date_part] + ([topic_slug] if topic_slug else []) + [suffix]
        slug = "_".join(parts)
        session_dir = workspace_dir / slug
        try:
            session_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            continue
        return SessionNewData(task_name=task_name, topic=topic, slug=slug, path=str(session_dir))

    raise SessionCreateFailedError(workspace_dir, MAX_SLUG_ATTEMPTS)


def build_session_new(cfg: Config, task_name: str, topic: str | None = None) -> Envelope[SessionNewData]:
    """CLI-facing entry point: resolves repo root, runs the operation, always returns an envelope."""

    try:
        data = create_session(cfg.repo_root(), task_name, topic)
    except SessionError as exc:
        return Envelope.build(
            SESSION_SCHEMA,
            SessionNewData(task_name=task_name, topic=topic, slug="", path=""),
            [EnvelopeError(code=exc.code, message=str(exc), detail=exc.detail)],
        )

    return Envelope.build(SESSION_SCHEMA, data)


def render_session_new_text(envelope: Envelope[SessionNewData]) -> str:
    if not envelope.ok:
        return "\n".join(f"error: {error.message}" for error in envelope.errors)
    return envelope.data.path
