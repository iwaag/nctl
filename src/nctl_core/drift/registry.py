"""Comparator registry (Phase 2 Step 3).

`register(resource_type)` decorates a comparator function so it runs on every
`run_comparators` call. Registration order must never affect output: which
module happens to import (and therefore register) first is an implementation
detail, not something `nctl drift`'s consumers should be able to observe. So
`run_comparators` always returns diff records sorted by
`(target.kind, target identity, code)`, regardless of registration order —
covered by a dedicated ordering-independence test.
"""

from __future__ import annotations

from typing import Callable, Iterable

from nctl_core.sources.snapshot import SourceSnapshot

from .context import DriftContext
from .model import DiffRecord

Comparator = Callable[[SourceSnapshot, DriftContext], Iterable[DiffRecord]]

_REGISTRY: dict[str, list[Comparator]] = {}


def register(resource_type: str) -> Callable[[Comparator], Comparator]:
    """Register `func` as a comparator for `resource_type` (e.g. "node", "service")."""

    def decorator(func: Comparator) -> Comparator:
        _REGISTRY.setdefault(resource_type, []).append(func)
        return func

    return decorator


def registered_resource_types() -> list[str]:
    return sorted(_REGISTRY)


def run_comparators(snapshot: SourceSnapshot, context: DriftContext) -> list[DiffRecord]:
    records: list[DiffRecord] = []
    for resource_type in sorted(_REGISTRY):
        for comparator in _REGISTRY[resource_type]:
            records.extend(comparator(snapshot, context))
    return sorted(records, key=_sort_key)


def _sort_key(record: DiffRecord) -> tuple[str, str, str, str, str]:
    target = record.target
    return (target.kind, target.slug or "", target.name or "", target.id or "", record.code)
