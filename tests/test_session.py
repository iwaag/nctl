"""Tests for `nctl_core.session` (`nctl session new`)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from nctl_core.session import (
    InvalidTaskNameError,
    SessionCreateFailedError,
    create_session,
)

NOW = datetime(2026, 7, 23, tzinfo=timezone.utc)


def test_create_session_without_topic(tmp_path: Path):
    data = create_session(tmp_path, "brainforge", now=NOW)
    assert data.task_name == "brainforge"
    assert data.topic is None
    assert data.slug.startswith("2026-07-23_")
    session_dir = Path(data.path)
    assert session_dir.is_dir()
    assert session_dir == tmp_path / ".local" / "workspace" / "brainforge" / data.slug


def test_create_session_with_topic_folds_into_slug(tmp_path: Path):
    data = create_session(tmp_path, "brainforge", topic="DNS Fix!", now=NOW)
    assert data.slug.startswith("2026-07-23_dns-fix_")


def test_create_session_two_calls_get_distinct_slugs(tmp_path: Path):
    first = create_session(tmp_path, "brainforge", now=NOW)
    second = create_session(tmp_path, "brainforge", now=NOW)
    assert first.slug != second.slug


def test_create_session_rejects_invalid_task_name(tmp_path: Path):
    with pytest.raises(InvalidTaskNameError):
        create_session(tmp_path, "Bad Name!", now=NOW)


def test_create_session_gives_up_after_repeated_collisions(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("nctl_core.session.secrets.token_hex", lambda n: "aaaa")
    create_session(tmp_path, "brainforge", now=NOW)
    with pytest.raises(SessionCreateFailedError):
        create_session(tmp_path, "brainforge", now=NOW)
