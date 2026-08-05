# Extending nctl_core

Developer-facing guide for adding modules, comparators, and reconcilers. See
[`../README.md`](../README.md#layout) for the package ownership map these rules protect.

## Module admission

A module is admitted for a responsibility, not to make another file shorter. Every new or split
module must:

1. own one operational value, contract, target set, route, identity, or lifecycle decision;
2. have a reason to change independent of the module it was separated from;
3. name its consumers;
4. belong to exactly one layer — transport, domain, orchestration, or presentation — and follow
   the dependency direction `presentation → orchestration → domain ← transport` without importing
   downward across it;
5. not exist solely to reduce line count;
6. not recreate a public schema deleted as an internal abstraction; and
7. have a documented entry in the [README's responsibility map](../README.md#layout).

An interface needs either two current implementations, or one current implementation and a second
one named in an approved roadmap. Treat line count only as a prompt to inspect ownership; it is
never the reason to split by itself.

## Adding a comparator

Comparators live under `src/nctl_core/drift/` and are registered by resource type:

```python
from nctl_core.drift.registry import register

@register("node")
def compare_example(snapshot, context):
    yield from ()
```

A comparator accepts one `SourceSnapshot` plus `DriftContext` and yields `DiffRecord` values. It
must not depend on registration order: the registry runs resource types deterministically and sorts
the combined output by target identity and diff code. Add focused comparator tests plus an engine
or `nctl.drift.v1` fixture whenever a new code affects target status or consumer behavior.

Compute realization is an active comparator example: `drift/compute_evaluation.py` owns the pure
platform/guest matching and field-comparison decision, while the thin `compute_instance`
registration in `comparators.py` attaches it to drift. Phase 1 deliberately classifies every
compute finding as manual review or unsupported: the evaluator may derive a candidate, but it
derives a unique existing guest candidate. A Phase 2 `ledger_patch` action may
record that candidate through the narrow compute-link API. A fully preflighted absent LXC is
instead planned as one `create_compute_instance` action, pinned to its control host and exact
`pct create` grammar; its handler re-derives those values and invokes only the bounded Proxmox
create playbook. Dry plans never invoke that handler or mutate a Proxmox guest.

## Adding a reconciler

Adding a reconciler changes the bounded plan/apply contract, so make each ownership point explicit:

1. Declare one stable `Reconciler` in `reconcile/reconcilers.py` through
   `reconcile/registry.py::register_reconciler()`. Its ID, default mutation posture, and action kind
   participate in the deterministic action DAG; add any dependency wiring where the planner builds
   the action.
2. Classify only its owned drift codes in `reconcile/classify.py`, then have
   `reconcile/planner.py::build_plan()` construct the `ReconcileAction`. The planner owns the exact
   target set. A handler consumes `action.targets` and must never widen it or substitute a convenient
   inventory group.
3. Implement one handler under `reconcile/actions/`. It receives `ActionContext` and returns
   `ExecutedAction`; use `ActionHandler` metadata to declare its `phase` (`bootstrap` or `service`)
   and whether it `needs_client`.
4. Register that handler in `reconcile/actions/dispatch.py`'s dispatch table. Keep expected
   `LedgerActionError`, `NautobotJobError`, and `NautobotError` translation there, where their code,
   mutation state, and durable action evidence are converted at the public action boundary.

Add focused planner, handler, and executor evidence tests. Do not create a placeholder reconciler,
handler, or dispatch entry: an inactive implementation is not a safe extension point.
