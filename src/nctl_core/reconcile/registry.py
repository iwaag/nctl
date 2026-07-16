"""Reconciler registry and action-DAG validation (Phase 4 Step 5).

Mirrors `drift/registry.py`'s shape: `register_reconciler` records metadata
keyed by a stable id, and lookups never depend on import/registration order.
Unlike the drift comparator registry (many functions run every time),
exactly one `Reconciler` handles a given diff code -- `classify.py` is the
single source of which id owns which code, not registration order here.
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import ReconcileAction


@dataclass(frozen=True)
class Reconciler:
    """Static identity/metadata for one registered reconciler.

    `mutates`/`requires_observation` are the reconciler's *default* posture,
    reused verbatim on every `ReconcileAction` it builds in this phase (a
    reconciler always mutates the same way; only whether it can act *at all*
    for a given target varies, which is decided per-instance in
    `reconcilers.py`, not here).
    """

    id: str
    action_kind: str
    mutates: bool
    requires_observation: bool


class DuplicateReconcilerError(Exception):
    pass


class UnknownReconcilerError(Exception):
    pass


class PlanCycleError(Exception):
    """`ReconcileAction.dependencies` forms a cycle -- refuse to plan (Decision 2)."""


_REGISTRY: dict[str, Reconciler] = {}


def register_reconciler(reconciler: Reconciler) -> Reconciler:
    if reconciler.id in _REGISTRY:
        raise DuplicateReconcilerError(f"reconciler {reconciler.id!r} is already registered")
    _REGISTRY[reconciler.id] = reconciler
    return reconciler


def get_reconciler(reconciler_id: str) -> Reconciler:
    try:
        return _REGISTRY[reconciler_id]
    except KeyError:
        raise UnknownReconcilerError(f"unknown reconciler id: {reconciler_id!r}") from None


def registered_reconciler_ids() -> list[str]:
    return sorted(_REGISTRY)


def topological_order(actions: list[ReconcileAction]) -> list[str]:
    """Return action ids in an order that respects `dependencies`.

    Deterministic regardless of input order: ties break on action id. Raises
    `PlanCycleError` for a cycle and `UnknownReconcilerError`-free but still
    fails loudly (`PlanCycleError`) for a dependency naming an action id that
    isn't in `actions` at all, since that can never be satisfied.
    """

    by_id = {action.id: action for action in actions}
    for action in actions:
        for dep in action.dependencies:
            if dep not in by_id:
                raise PlanCycleError(
                    f"action {action.id!r} depends on {dep!r}, which is not in this plan"
                )

    visited: set[str] = set()
    in_progress: set[str] = set()
    ordered: list[str] = []

    def visit(action_id: str, path: tuple[str, ...]) -> None:
        if action_id in visited:
            return
        if action_id in in_progress:
            raise PlanCycleError(f"dependency cycle: {' -> '.join((*path, action_id))}")
        in_progress.add(action_id)
        for dep in sorted(by_id[action_id].dependencies):
            visit(dep, (*path, action_id))
        in_progress.discard(action_id)
        visited.add(action_id)
        ordered.append(action_id)

    for action_id in sorted(by_id):
        visit(action_id, ())
    return ordered
