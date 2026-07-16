from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from nctl_core.config import Config
from nctl_core.drift.engine import DriftResult, TargetStatus
from nctl_core.drift.model import DiffRecord, Severity, Status, Target
from nctl_core.output import Envelope, EnvelopeError
from nctl_core.reconcile import executor as executor_module
from nctl_core.reconcile.executor import run_reconcile
from nctl_core.reconcile.ledger import IpamReconcileResult, LinkActualNodeResult
from nctl_core.reconcile.lock import acquire_reconcile_lock
from nctl_core.sources.actual import ActualSnapshot
from nctl_core.sources.desired import DesiredNode, DesiredSnapshot
from nctl_core.sources.snapshot import SourceSnapshot


def _config(tmp_path):
    ansible_dir = tmp_path / "ansible_agdev"
    (ansible_dir / "playbooks").mkdir(parents=True)
    inventory = ansible_dir / "inventories/generated/hosts_intent.yml"
    inventory.parent.mkdir(parents=True)
    inventory.write_text("all: {}\n")
    config_path = tmp_path / "nctl.toml"
    config_path.write_text(
        f"""
[nautobot]
url = "http://nautobot.test"

[inventory]
dumps_dir = "{tmp_path / 'dumps'}"

[events]
log_dir = "{tmp_path / 'events'}"

[ansible]
playbook_dir = "{ansible_dir}"
inventory = "inventories/generated/hosts_intent.yml"

[reconcile]
max_rounds = 3
lock_path = "{tmp_path / 'reconcile.lock'}"

[repo]
root = "{tmp_path}"
"""
    )
    return Config.load(config_path)


def _snapshot(nodes=()) -> SourceSnapshot:
    return SourceSnapshot(
        desired=DesiredSnapshot(nodes=list(nodes)),
        actual=ActualSnapshot(),
        fetched_at=datetime.now(timezone.utc),
    )


def _node(slug="agweb") -> DesiredNode:
    return DesiredNode(
        id="11111111-1111-1111-1111-111111111111",
        slug=slug,
        name=slug,
        lifecycle="active",
        node_type="device",
        accepted_actual_types=["device"],
    )


def _target_status(target, status, diffs=()) -> TargetStatus:
    return TargetStatus(target=target, status=status, diffs=list(diffs))


def _drift(targets, *, generated_at="2026-07-17T00:00:00+00:00") -> tuple[SourceSnapshot, DriftResult, str]:
    snapshot = _snapshot(nodes=[_node()])
    summary: dict[str, int] = {}
    for t in targets:
        summary[t.status.value] = summary.get(t.status.value, 0) + 1
    return snapshot, DriftResult(summary=summary, targets=targets), generated_at


def _no_op_deployment_profiles(monkeypatch):
    monkeypatch.setattr(executor_module, "load_deployment_profiles", lambda playbook_dir: ({}, "digest"))
    monkeypatch.setattr(executor_module, "load_profile_reconciliation", lambda playbook_dir, names: {})


def _stub_dashboard(monkeypatch, *, ok=True):
    def fake(cfg, drift_envelope, **kwargs):
        return Envelope.build("nctl.dashboard.v1", executor_module.DashboardData(), [] if ok else [EnvelopeError(code="x", message="dashboard failed")])

    monkeypatch.setattr(executor_module, "render_dashboard_from_drift", fake)


def _sequence(monkeypatch, results):
    it = iter(results)
    monkeypatch.setattr(executor_module, "fetch_and_compute_drift", lambda cfg: next(it))


# --- plan mode --------------------------------------------------------------


def test_plan_mode_never_mutates_and_reports_planned(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _no_op_deployment_profiles(monkeypatch)
    node = _node()
    diff = DiffRecord(target=Target(kind="node", slug=node.slug, name=node.name, id=node.id), code="actual_node_not_linked", severity=Severity.ERROR, message="x")
    _sequence(monkeypatch, [_drift([_target_status(diff.target, Status.DRIFTING, [diff])])])

    called = {"n": 0}
    monkeypatch.setattr(executor_module, "execute_link_actual_node", lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    envelope = run_reconcile(cfg, apply_changes=False)

    assert envelope.data.state == "planned"
    assert envelope.ok
    assert called["n"] == 0
    assert envelope.data.plan_path
    assert (tmp_path / "events" / envelope.data.operation_id / "plan.json").is_file()


# --- already converged -------------------------------------------------------


def test_already_converged_when_no_diffs(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _no_op_deployment_profiles(monkeypatch)
    _sequence(monkeypatch, [_drift([])])
    _stub_dashboard(monkeypatch)

    envelope = run_reconcile(cfg, apply_changes=True)

    assert envelope.data.state == "already_converged"
    assert envelope.ok
    assert envelope.data.rounds == []


# --- manual block ------------------------------------------------------------


def test_manual_review_blocks_before_any_mutation(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _no_op_deployment_profiles(monkeypatch)
    node = _node()
    diff = DiffRecord(target=Target(kind="node", slug=node.slug, name=node.name, id=node.id), code="ambiguous_actual_node_candidates", severity=Severity.ERROR, message="x")
    _sequence(monkeypatch, [_drift([_target_status(diff.target, Status.DRIFTING, [diff])])])
    _stub_dashboard(monkeypatch)
    calls = {"n": 0}
    monkeypatch.setattr(executor_module, "AnsibleRunner", lambda *a, **k: calls.__setitem__("n", calls["n"] + 1))

    envelope = run_reconcile(cfg, apply_changes=True)

    assert envelope.data.state == "manual_intervention_required"
    assert not envelope.ok
    assert envelope.data.rounds == []
    assert len(envelope.data.manual_review) == 1
    assert calls["n"] == 0


# --- no-progress / max-round stop -------------------------------------------


def test_no_progress_stops_before_max_rounds(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _no_op_deployment_profiles(monkeypatch)
    node = _node()
    diff = DiffRecord(target=Target(kind="node", slug=node.slug, name=node.name, id=node.id), code="missing_actual_data", severity=Severity.ERROR, message="x")
    drift = _drift([_target_status(diff.target, Status.UNKNOWN, [diff])])
    _sequence(monkeypatch, [drift, drift, drift])  # identical fingerprint every round
    _stub_dashboard(monkeypatch)
    monkeypatch.setattr(
        executor_module,
        "run_observation",
        lambda *a, **k: executor_module.ObservationResult(ok=False, hosts=[], collection=_fake_ansible_result(), retrieval=_fake_ansible_result()),
    )

    envelope = run_reconcile(cfg, apply_changes=True, max_rounds=5)

    assert envelope.data.state == "non_converged"
    assert any(e.code == "no_progress" for e in envelope.errors)
    assert len(envelope.data.rounds) == 1  # round 0 executes; round 1's plan repeats the fingerprint and stops


def _fake_ansible_result():
    from nctl_core.ansible import AnsibleRunResult

    return AnsibleRunResult(mode="collect", exit_code=0)


def test_max_rounds_reached_when_progress_never_completes(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _no_op_deployment_profiles(monkeypatch)
    node = _node()

    def make_drift(code):
        diff = DiffRecord(target=Target(kind="node", slug=node.slug, name=node.name, id=node.id), code=code, severity=Severity.ERROR, message="x")
        return _drift([_target_status(diff.target, Status.UNKNOWN, [diff])])

    # A different code each round keeps the fingerprint changing, so it never
    # hits the no-progress check and instead exhausts max_rounds.
    _sequence(monkeypatch, [make_drift("missing_actual_data"), make_drift("stale_actual_data"), make_drift("invalid_actual_timestamp")])
    _stub_dashboard(monkeypatch)
    monkeypatch.setattr(
        executor_module,
        "run_observation",
        lambda *a, **k: executor_module.ObservationResult(ok=True, hosts=[], collection=_fake_ansible_result(), retrieval=_fake_ansible_result()),
    )

    envelope = run_reconcile(cfg, apply_changes=True, max_rounds=3)

    assert envelope.data.state == "non_converged"
    assert any(e.code == "max_rounds_reached" for e in envelope.errors)
    assert len(envelope.data.rounds) == 3


# --- lock contention ----------------------------------------------------------


def test_lock_contention_fails_without_planning(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    with acquire_reconcile_lock(cfg.reconcile.resolved_lock_path()):
        called = {"n": 0}
        monkeypatch.setattr(executor_module, "fetch_and_compute_drift", lambda cfg: called.__setitem__("n", called["n"] + 1))

        envelope = run_reconcile(cfg, apply_changes=True)

    assert envelope.data.state == "failed"
    assert any(e.code == "reconcile_lock_contention" for e in envelope.errors)
    assert called["n"] == 0


# --- interruption -------------------------------------------------------------


def test_interrupted_before_round_reports_failed(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _no_op_deployment_profiles(monkeypatch)
    node = _node()
    diff = DiffRecord(target=Target(kind="node", slug=node.slug, name=node.name, id=node.id), code="actual_node_not_linked", severity=Severity.ERROR, message="x")
    _sequence(monkeypatch, [_drift([_target_status(diff.target, Status.DRIFTING, [diff])])])

    class AlwaysInterrupted:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def is_set(self):
            return True

    monkeypatch.setattr(executor_module, "_InterruptFlag", AlwaysInterrupted)

    envelope = run_reconcile(cfg, apply_changes=True)

    assert envelope.data.state == "failed"
    assert any(e.code == "interrupted" for e in envelope.errors)
    assert envelope.data.rounds == []


# --- ledger action executes and converges ------------------------------------


def test_link_actual_node_action_executes_and_converges_next_round(tmp_path, monkeypatch):
    from nctl_core.sources.actual import ActualDevice

    cfg = _config(tmp_path)
    _no_op_deployment_profiles(monkeypatch)
    node = _node()
    diff = DiffRecord(target=Target(kind="node", slug=node.slug, name=node.name, id=node.id), code="actual_node_not_linked", severity=Severity.ERROR, message="x")

    def snapshot_with_candidate():
        snapshot = _snapshot(nodes=[node])
        snapshot.actual = ActualSnapshot(devices=[ActualDevice(id="dev-1", name=node.name)])
        return snapshot

    def make_drift(status, diffs):
        targets = [_target_status(diff.target, status, diffs)]
        summary = {targets[0].status.value: 1}
        return snapshot_with_candidate(), DriftResult(summary=summary, targets=targets), "2026-07-17T00:00:00+00:00"

    drifting = make_drift(Status.DRIFTING, [diff])
    converged = make_drift(Status.CONVERGED, [])
    _sequence(monkeypatch, [drifting, converged])
    _stub_dashboard(monkeypatch)

    link_calls = []
    monkeypatch.setattr(
        executor_module,
        "execute_link_actual_node",
        lambda client, action: link_calls.append(action.id) or LinkActualNodeResult(
            node_id=node.id, node_slug=node.slug, field="realized_device", candidate_id="dev-1"
        ),
    )

    envelope = run_reconcile(cfg, apply_changes=True)

    assert envelope.data.state == "converged"
    assert envelope.ok
    assert len(link_calls) == 1
    assert len(envelope.data.rounds) == 1
    [action_result] = [a for a in envelope.data.rounds[0].actions if a.reconciler_id == "link_actual_node"]
    assert action_result.success is True


# --- independent partial failure among service actions -----------------------


def test_independent_service_action_failure_does_not_block_the_other(tmp_path, monkeypatch):
    cfg = _config(tmp_path)

    def fake_load_profiles(playbook_dir):
        return (
            {
                "good": {"group": "good_server", "config_schema_version": "1", "variables": {}},
                "bad": {"group": "bad_server", "config_schema_version": "1", "variables": {}},
            },
            "digest",
        )

    monkeypatch.setattr(executor_module, "load_deployment_profiles", fake_load_profiles)

    from nctl_core.reconcile.profiles import ProfileAction, ProfileReconciliation

    monkeypatch.setattr(
        executor_module,
        "load_profile_reconciliation",
        lambda playbook_dir, names: {
            "good": ProfileReconciliation(action=ProfileAction(kind="playbook", playbook="playbooks/good.yml")),
            "bad": ProfileReconciliation(action=ProfileAction(kind="playbook", playbook="playbooks/bad.yml")),
        },
    )
    from nctl_core.production_render import ProductionRenderData

    monkeypatch.setattr(
        executor_module, "build_production_render", lambda cfg: Envelope.build("nctl.render.production.v1", ProductionRenderData(), [])
    )
    monkeypatch.setattr(executor_module, "write_production_artifacts", lambda envelope, out_dir: None)
    _stub_dashboard(monkeypatch)

    node = _node()
    good_service = _service_and_placement("good-svc", "good", node)
    bad_service = _service_and_placement("bad-svc", "bad", node)

    good_diff = DiffRecord(
        target=Target(kind="service", slug="good-svc", name="good-svc", id="s-good"),
        code="service_not_running",
        severity=Severity.ERROR,
        message="x",
    )
    bad_diff = DiffRecord(
        target=Target(kind="service", slug="bad-svc", name="bad-svc", id="s-bad"),
        code="service_not_running",
        severity=Severity.ERROR,
        message="x",
    )

    def make_snapshot():
        snapshot = _snapshot(nodes=[node])
        snapshot.desired.services = [good_service[0], bad_service[0]]
        snapshot.desired.placements = [good_service[1], bad_service[1]]
        return snapshot

    def drift_with(snapshot, targets):
        summary: dict[str, int] = {}
        for t in targets:
            summary[t.status.value] = summary.get(t.status.value, 0) + 1
        return snapshot, DriftResult(summary=summary, targets=targets), "2026-07-17T00:00:00+00:00"

    round0 = drift_with(
        make_snapshot(),
        [
            _target_status(good_diff.target, Status.DRIFTING, [good_diff]),
            _target_status(bad_diff.target, Status.DRIFTING, [bad_diff]),
        ],
    )
    round1 = drift_with(
        make_snapshot(),
        [
            _target_status(good_diff.target, Status.CONVERGED, []),
            _target_status(bad_diff.target, Status.DRIFTING, [bad_diff]),
        ],
    )
    round2 = drift_with(
        make_snapshot(),
        [
            _target_status(good_diff.target, Status.CONVERGED, []),
            _target_status(bad_diff.target, Status.DRIFTING, [bad_diff]),
        ],
    )
    _sequence(monkeypatch, [round0, round1, round2])

    def fake_runner_run(self, args, *, mode, artifact_stem=None):
        from nctl_core.ansible import AnsibleRunResult

        ok = "bad.yml" not in " ".join(args)
        return AnsibleRunResult(mode=mode, command=args, exit_code=0 if ok else 1)

    monkeypatch.setattr(executor_module.AnsibleRunner, "run", fake_runner_run)

    envelope = run_reconcile(cfg, apply_changes=True, max_rounds=3)

    round0_actions = envelope.data.rounds[0].actions
    successes = {a.success for a in round0_actions if a.reconciler_id == "service_profile"}
    assert successes == {True, False}
    assert envelope.data.state == "non_converged"


def _service_and_placement(slug, profile, node):
    from nctl_core.sources.desired import DesiredService, DesiredServicePlacement

    service = DesiredService(
        id=f"svc-{slug}",
        slug=slug,
        name=slug,
        display_name=slug,
        service_type="daemon",
        lifecycle="active",
        catalog_namespace="ns",
        catalog_metadata_name=slug,
    )
    placement = DesiredServicePlacement(
        id=f"p-{slug}",
        service_id=service.id,
        node_id=node.id,
        instance_name=f"{profile}-{node.id}",
        deployment_profile=profile,
        config_schema_version="1",
    )
    return service, placement


# --- dashboard degradation -----------------------------------------------------


def test_dashboard_failure_does_not_overwrite_terminal_state(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _no_op_deployment_profiles(monkeypatch)
    _sequence(monkeypatch, [_drift([])])
    _stub_dashboard(monkeypatch, ok=False)

    envelope = run_reconcile(cfg, apply_changes=True)

    assert envelope.data.state == "already_converged"
    assert envelope.ok
    events = [
        json.loads(line)
        for line in (tmp_path / "events" / f"{envelope.data.operation_id}.jsonl").read_text().splitlines()
    ]
    assert any(e["event"] == "warning" for e in events)


# --- unknown host -------------------------------------------------------------


def test_unknown_host_reports_failed_with_code(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _no_op_deployment_profiles(monkeypatch)
    _sequence(monkeypatch, [_drift([])])

    envelope = run_reconcile(cfg, host="ghost-host", apply_changes=False)

    assert envelope.data.state == "failed"
    assert any(e.code == "unknown_host" for e in envelope.errors)
