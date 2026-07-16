from __future__ import annotations

import pytest

from nctl_core.drift.model import Target
from nctl_core.reconcile.model import ReconcileAction
from nctl_core.reconcile.registry import (
    PlanCycleError,
    UnknownReconcilerError,
    get_reconciler,
    registered_reconciler_ids,
    topological_order,
)


def _action(action_id: str, dependencies: list[str] | None = None) -> ReconcileAction:
    return ReconcileAction(
        id=action_id,
        reconciler_id="observe_node",
        action_kind="observation",
        targets=[Target(kind="node", slug="agweb")],
        claimed_diff_codes=["ingest_lag"],
        reason="test",
        dependencies=dependencies or [],
        mutates=True,
        requires_observation=False,
    )


def test_initial_reconcilers_are_registered():
    # reconcilers.py registers these at import time; importing it here (via
    # planner's transitive import in other test modules) is not guaranteed,
    # so import it explicitly.
    import nctl_core.reconcile.reconcilers  # noqa: F401

    ids = registered_reconciler_ids()
    for expected in (
        "observe_node",
        "link_actual_node",
        "reconcile_ipam",
        "service_profile",
        "dnsmasq_config",
        "new_node_baseline",
    ):
        assert expected in ids
        assert get_reconciler(expected).id == expected


def test_unknown_reconciler_id_raises():
    with pytest.raises(UnknownReconcilerError):
        get_reconciler("does_not_exist")


def test_topological_order_respects_dependencies():
    a = _action("a")
    b = _action("b", ["a"])
    c = _action("c", ["b"])

    order = topological_order([c, a, b])

    assert order.index("a") < order.index("b") < order.index("c")


def test_topological_order_is_deterministic_regardless_of_input_order():
    a = _action("a")
    b = _action("b")
    c = _action("c")

    order1 = topological_order([c, b, a])
    order2 = topological_order([a, b, c])

    assert order1 == order2 == ["a", "b", "c"]


def test_cycle_raises_plan_cycle_error():
    a = _action("a", ["b"])
    b = _action("b", ["a"])

    with pytest.raises(PlanCycleError):
        topological_order([a, b])


def test_dependency_on_missing_action_raises():
    a = _action("a", ["ghost"])

    with pytest.raises(PlanCycleError):
        topological_order([a])


def test_self_dependency_is_a_cycle():
    a = _action("a", ["a"])

    with pytest.raises(PlanCycleError):
        topological_order([a])
