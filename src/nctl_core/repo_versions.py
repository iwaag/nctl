"""Resolve reproducible component versions owned by the superproject."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from nctl_core.config import Config

_FULL_GIT_OBJECT_ID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


class RepoVersionError(ValueError):
    """A required, superproject-owned component version cannot be resolved."""


def resolve_nodeutils_version(cfg: Config) -> str:
    """Return the exact nodeutils commit compatible with this checkout.

    A full-SHA configuration override is useful for packaged controllers that
    do not retain the superproject metadata. Normal source checkouts use the
    nodeutils gitlink recorded by the superproject's current commit.
    """

    if cfg.reconcile.nodeutils_version is not None:
        return cfg.reconcile.nodeutils_version
    return resolve_gitlink_commit(cfg.repo_root(), "nodeutils")


def resolve_gitlink_commit(repo_root: Path, submodule_path: str) -> str:
    """Resolve one submodule gitlink without trusting its working-tree HEAD."""

    try:
        completed = subprocess.run(
            ["git", "ls-tree", "HEAD", "--", submodule_path],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RepoVersionError(
            f"cannot resolve pinned {submodule_path} version from {repo_root}: {exc}"
        ) from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"git exited {completed.returncode}"
        raise RepoVersionError(
            f"cannot resolve pinned {submodule_path} version from {repo_root}: {detail}"
        )

    line = completed.stdout.strip()
    fields = line.split(maxsplit=3)
    if len(fields) != 4 or fields[0] != "160000" or fields[1] != "commit":
        raise RepoVersionError(
            f"{repo_root}/{submodule_path} is not a gitlink in the superproject HEAD"
        )
    commit = fields[2].lower()
    if not _FULL_GIT_OBJECT_ID.fullmatch(commit):
        raise RepoVersionError(
            f"superproject gitlink for {submodule_path} is not a full Git object ID"
        )
    return commit
