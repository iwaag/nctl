"""Comparator framework and drift core (Phase 2 Step 3).

A comparator is a function `(SourceSnapshot, DriftContext) -> Iterable[DiffRecord]`
registered against a resource type via `registry.register`. `engine.compute_drift`
runs every registered comparator, groups the resulting diff records into one
`TargetStatus` per target (seeded so a target with zero diffs still reports
`converged`), and derives each target's status. CLI/Config/Envelope wiring
(`nctl drift`) is Step 5's job — this package only needs a `SourceSnapshot`.
"""
