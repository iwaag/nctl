from __future__ import annotations

import pytest

from nctl_core.reconcile.lock import ReconcileLockError, acquire_reconcile_lock


def test_lock_can_be_acquired_and_released(tmp_path):
    path = tmp_path / "reconcile.lock"
    with acquire_reconcile_lock(path):
        pass
    # Released on exit: a second acquisition must succeed.
    with acquire_reconcile_lock(path):
        pass


def test_concurrent_acquisition_raises_lock_error(tmp_path):
    path = tmp_path / "reconcile.lock"
    with acquire_reconcile_lock(path):
        with pytest.raises(ReconcileLockError):
            with acquire_reconcile_lock(path):
                pass


def test_lock_creates_parent_directories(tmp_path):
    path = tmp_path / "nested" / "dir" / "reconcile.lock"
    with acquire_reconcile_lock(path):
        assert path.parent.is_dir()


def test_lock_released_even_if_body_raises(tmp_path):
    path = tmp_path / "reconcile.lock"
    with pytest.raises(ValueError):
        with acquire_reconcile_lock(path):
            raise ValueError("boom")

    with acquire_reconcile_lock(path):
        pass
