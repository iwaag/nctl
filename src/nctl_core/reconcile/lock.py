"""One controller-local reconcile lock (Phase 4 Step 7).

Phase 4 deliberately permits only one mutating `nctl reconcile --yes` at a
time (Phase 5 can replace this with a server-side operation lock once a
daemon exists). A plain `flock` on a fixed path is enough for a LAN-only
single-controller tool: it is released automatically if the process dies,
needs no cleanup step, and works identically for a human, cron, or an AI
caller.
"""

from __future__ import annotations

import fcntl
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class ReconcileLockError(Exception):
    """Another `nctl reconcile --yes` already holds the lock."""


@contextmanager
def acquire_reconcile_lock(path: Path) -> Iterator[None]:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ReconcileLockError(f"another reconcile operation holds the lock at {path}") from exc
        yield
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            handle.close()
