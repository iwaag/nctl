from __future__ import annotations

import pytest

from nctl_core.drift import registry
from nctl_core.drift.context import DriftContext
from nctl_core.drift.model import DiffRecord, Severity, Target


@pytest.fixture
def isolated_registry(monkeypatch):
    """Comparator registration is process-global; isolate it per test so
    registering a fixture comparator here can't leak into other tests."""
    monkeypatch.setattr(registry, "_REGISTRY", {})
    return registry


def _record(kind: str, ident: str, code: str) -> DiffRecord:
    return DiffRecord(
        target=Target(kind=kind, id=ident),
        code=code,
        severity=Severity.ERROR,
        message="test",
    )


def test_run_comparators_output_is_independent_of_registration_order(isolated_registry):
    @isolated_registry.register("node")
    def second(snapshot, context):
        yield _record("node", "a", "z_code")

    @isolated_registry.register("node")
    def first(snapshot, context):
        yield _record("node", "a", "a_code")

    context = DriftContext(generated_at="2026-07-15T00:00:00+00:00")
    records = isolated_registry.run_comparators(None, context)

    assert [r.code for r in records] == ["a_code", "z_code"]


def test_run_comparators_sorts_by_target_then_code(isolated_registry):
    @isolated_registry.register("node")
    def comparator(snapshot, context):
        yield _record("node", "b", "code1")
        yield _record("node", "a", "code2")
        yield _record("node", "a", "code1")

    context = DriftContext(generated_at="2026-07-15T00:00:00+00:00")
    records = isolated_registry.run_comparators(None, context)

    assert [(r.target.id, r.code) for r in records] == [("a", "code1"), ("a", "code2"), ("b", "code1")]


def test_registered_resource_types_reflects_registrations(isolated_registry):
    @isolated_registry.register("service")
    def comparator(snapshot, context):
        return []

    assert isolated_registry.registered_resource_types() == ["service"]
