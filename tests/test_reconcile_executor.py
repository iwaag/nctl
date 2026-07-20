from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from nctl_core.config import Config
from nctl_core.drift.engine import DriftResult, TargetStatus
from nctl_core.drift.model import DiffRecord, Severity, Status, Target
from nctl_core.output import Envelope, EnvelopeError
from nctl_core.reconcile import executor as executor_module
from nctl_core.reconcile.executor import run_reconcile
from nctl_core.reconcile.ledger import IpamReconcileResult, LinkActualNodeResult
from nctl_core.reconcile.lock import acquire_reconcile_lock
from nctl_core.reconcile.model import ReconcileAction
from nctl_core.sources.actual import ActualDevice, ActualSnapshot
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


def test_playbook_grouping_passes_the_fixed_operation_timestamp_to_resolver(monkeypatch):
    node = _node("agweb").model_copy(update={"realized_device_id": "dev-1"})
    snapshot = SourceSnapshot(
        desired=DesiredSnapshot(nodes=[node]),
        actual=ActualSnapshot(devices=[ActualDevice(id="dev-1", name="agweb.local")]),
        fetched_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    action = ReconcileAction(
        id="service_profile:web", reconciler_id="service_profile", action_kind="playbook",
        targets=[Target(kind="service", slug="web", id="s1")],
        claimed_diff_codes=["service_not_running"], reason="test", mutates=True,
        requires_observation=False,
        parameters={"playbook_by_os": {"linux": "playbooks/linux.yml"}},
    )
    seen = {}

    def fake_resolve(**kwargs):
        seen["generated_at"] = kwargs["generated_at"]
        return SimpleNamespace(host_os=SimpleNamespace(value="linux"))

    monkeypatch.setattr(executor_module, "resolve_operational_values", fake_resolve)

    groups = executor_module._group_hosts_by_playbook(
        action, ["agweb"], snapshot, generated_at="2026-07-20T12:34:56+00:00"
    )

    assert groups == {"playbooks/linux.yml": ["agweb"]}
    assert seen["generated_at"] == "2026-07-20T12:34:56+00:00"


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


def test_terminal_result_json_is_persisted_publicly_and_matches_the_envelope(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _no_op_deployment_profiles(monkeypatch)
    node = _node()
    diff = DiffRecord(target=Target(kind="node", slug=node.slug, name=node.name, id=node.id), code="actual_node_not_linked", severity=Severity.ERROR, message="x")
    _sequence(monkeypatch, [_drift([_target_status(diff.target, Status.DRIFTING, [diff])])])
    monkeypatch.setattr(executor_module, "execute_link_actual_node", lambda *a, **k: None)

    envelope = run_reconcile(cfg, apply_changes=False)

    result_path = tmp_path / "events" / envelope.data.operation_id / "result.json"
    assert result_path.is_file()
    assert result_path.stat().st_mode & 0o777 == 0o644
    payload = json.loads(result_path.read_text())
    assert payload["schema"] == "nctl.reconcile.v1"
    assert payload["ok"] == envelope.ok
    assert payload["data"]["state"] == envelope.data.state


def test_operation_id_can_be_pre_assigned_by_a_caller(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _no_op_deployment_profiles(monkeypatch)
    _sequence(monkeypatch, [_drift([])])
    _stub_dashboard(monkeypatch)

    envelope = run_reconcile(cfg, apply_changes=True, operation_id="01ARZ3NDEKTSV4RRFFQ69G5FAV")

    assert envelope.data.operation_id == "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    assert (tmp_path / "events" / "01ARZ3NDEKTSV4RRFFQ69G5FAV.jsonl").is_file()


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


# --- observe_node target resolution -------------------------------------------


def test_observe_node_action_only_receives_node_slugs_for_service_diffs(tmp_path, monkeypatch):
    """Regression for the fix1 bug: a service-kind evidence-gap diff must resolve
    to its owning node before reaching run_observation, not fail with
    'hosts are not bootstrap-eligible: <service-slug>'.
    """
    cfg = _config(tmp_path)
    _no_op_deployment_profiles(monkeypatch)
    node = _node("agdnsmasq")
    diff = DiffRecord(
        target=Target(kind="service", slug="dnsmasq", name="dnsmasq", id="s1"),
        code="service_observation_missing",
        severity=Severity.ERROR,
        message="dnsmasq: service_observation_missing",
        desired={"expected": {"node_slug": node.slug, "node_id": node.id}},
    )
    _sequence(monkeypatch, [_drift([_target_status(diff.target, Status.UNKNOWN, [diff])])])
    _stub_dashboard(monkeypatch)

    captured = {}

    def fake_run_observation(*a, **kwargs):
        captured["target_slugs"] = kwargs.get("target_slugs") or a[2]
        return executor_module.ObservationResult(ok=True, hosts=[], collection=_fake_ansible_result(), retrieval=_fake_ansible_result())

    monkeypatch.setattr(executor_module, "run_observation", fake_run_observation)

    envelope = run_reconcile(cfg, apply_changes=True, max_rounds=1)

    assert captured["target_slugs"] == ["agdnsmasq"]
    assert envelope.data.rounds[0].actions[0].success is True


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


# --- Phase 1 (better_usability p1): local-blocker orchestration matrix -------
#
# Decision 5: a global finding stops every action; a target-local finding
# blocks only its own target. Independent healthy-target actions still run,
# and once independent progress is exhausted a still-present local blocker
# reports the true manual_intervention_required reason (never a misleading
# max_rounds_reached).


def test_local_blocker_allows_independent_action_then_reports_manual_intervention(tmp_path, monkeypatch):
    from nctl_core.sources.actual import ActualDevice

    cfg = _config(tmp_path)
    _no_op_deployment_profiles(monkeypatch)
    healthy = _node("aghealthy")
    blocked = DesiredNode(
        id="22222222-2222-2222-2222-222222222222", slug="agblocked", name="agblocked",
        lifecycle="active", node_type="device", accepted_actual_types=["device"],
    )
    link_diff = DiffRecord(
        target=Target(kind="node", slug=healthy.slug, name=healthy.name, id=healthy.id),
        code="actual_node_not_linked", severity=Severity.ERROR, message="x",
    )
    local_diff = DiffRecord(
        target=Target(kind="node", slug=blocked.slug, name=blocked.name, id=blocked.id),
        code="unresolved_connection_path", severity=Severity.ERROR, message="agblocked: unresolved_connection_path",
    )

    def snapshot_with_candidate():
        snapshot = _snapshot(nodes=[healthy, blocked])
        snapshot.actual = ActualSnapshot(devices=[ActualDevice(id="dev-1", name=healthy.name)])
        return snapshot

    def make_drift(link_status, link_diffs):
        targets = [
            _target_status(link_diff.target, link_status, link_diffs),
            _target_status(local_diff.target, Status.DRIFTING, [local_diff]),
        ]
        summary: dict[str, int] = {}
        for t in targets:
            summary[t.status.value] = summary.get(t.status.value, 0) + 1
        return snapshot_with_candidate(), DriftResult(summary=summary, targets=targets), "2026-07-17T00:00:00+00:00"

    round0 = make_drift(Status.DRIFTING, [link_diff])
    round1 = make_drift(Status.CONVERGED, [])
    _sequence(monkeypatch, [round0, round1])
    _stub_dashboard(monkeypatch)

    link_calls = []
    monkeypatch.setattr(
        executor_module,
        "execute_link_actual_node",
        lambda client, action: link_calls.append(action.id) or LinkActualNodeResult(
            node_id=healthy.id, node_slug=healthy.slug, field="realized_device", candidate_id="dev-1"
        ),
    )

    envelope = run_reconcile(cfg, apply_changes=True, max_rounds=5)

    assert len(link_calls) == 1  # the healthy node's independent action still executed
    assert envelope.data.state == "manual_intervention_required"
    assert not envelope.ok
    assert len(envelope.data.rounds) == 1
    assert envelope.data.progress_made is True
    assert any(r["code"] == "unresolved_connection_path" for r in envelope.data.manual_review)


def test_local_blocker_with_no_actions_terminates_without_mutation(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _no_op_deployment_profiles(monkeypatch)
    node = _node("agblocked")
    diff = DiffRecord(
        target=Target(kind="node", slug=node.slug, name=node.name, id=node.id),
        code="unresolved_connection_path", severity=Severity.ERROR, message="x",
    )
    _sequence(monkeypatch, [_drift([_target_status(diff.target, Status.DRIFTING, [diff])])])
    _stub_dashboard(monkeypatch)
    calls = {"n": 0}
    monkeypatch.setattr(executor_module, "AnsibleRunner", lambda *a, **k: calls.__setitem__("n", calls["n"] + 1))

    envelope = run_reconcile(cfg, apply_changes=True)

    assert envelope.data.state == "manual_intervention_required"
    assert not envelope.ok
    assert envelope.data.rounds == []
    assert envelope.data.progress_made is False
    assert calls["n"] == 0


def test_global_blocker_stops_before_any_action_even_with_actionable_drift(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _no_op_deployment_profiles(monkeypatch)
    node = _node()
    link_diff = DiffRecord(
        target=Target(kind="node", slug=node.slug, name=node.name, id=node.id),
        code="actual_node_not_linked", severity=Severity.ERROR, message="x",
    )
    global_diff = DiffRecord(
        target=Target(kind="global"), code="unknown_profile", severity=Severity.ERROR, message="broken profile map",
    )
    _sequence(
        monkeypatch,
        [
            _drift(
                [
                    _target_status(link_diff.target, Status.DRIFTING, [link_diff]),
                    _target_status(Target(kind="global"), Status.DRIFTING, [global_diff]),
                ]
            )
        ],
    )
    _stub_dashboard(monkeypatch)
    calls = {"n": 0}
    monkeypatch.setattr(
        executor_module, "execute_link_actual_node", lambda *a, **k: calls.__setitem__("n", calls["n"] + 1)
    )

    envelope = run_reconcile(cfg, apply_changes=True)

    assert envelope.data.state == "manual_intervention_required"
    assert envelope.data.rounds == []
    assert calls["n"] == 0


def test_max_rounds_reached_with_a_known_local_blocker_reports_manual_intervention(tmp_path, monkeypatch):
    # Independent progress runs out exactly on the last permitted round: the
    # terminal reason must be the true manual_intervention_required, not a
    # misleading max_rounds_reached (plan Step 1.5's round-limit edge case).
    from nctl_core.sources.actual import ActualDevice

    cfg = _config(tmp_path)
    _no_op_deployment_profiles(monkeypatch)
    healthy = _node("aghealthy")
    blocked = DesiredNode(
        id="33333333-3333-3333-3333-333333333333", slug="agblocked", name="agblocked",
        lifecycle="active", node_type="device", accepted_actual_types=["device"],
    )
    link_diff = DiffRecord(
        target=Target(kind="node", slug=healthy.slug, name=healthy.name, id=healthy.id),
        code="actual_node_not_linked", severity=Severity.ERROR, message="x",
    )
    local_diff = DiffRecord(
        target=Target(kind="node", slug=blocked.slug, name=blocked.name, id=blocked.id),
        code="invalid_platform_power", severity=Severity.ERROR, message="agblocked: invalid_platform_power",
    )

    def snapshot_with_candidate():
        snapshot = _snapshot(nodes=[healthy, blocked])
        snapshot.actual = ActualSnapshot(devices=[ActualDevice(id="dev-1", name=healthy.name)])
        return snapshot

    def make_drift(link_status, link_diffs):
        targets = [
            _target_status(link_diff.target, link_status, link_diffs),
            _target_status(local_diff.target, Status.DRIFTING, [local_diff]),
        ]
        summary: dict[str, int] = {}
        for t in targets:
            summary[t.status.value] = summary.get(t.status.value, 0) + 1
        return snapshot_with_candidate(), DriftResult(summary=summary, targets=targets), "2026-07-17T00:00:00+00:00"

    round0 = make_drift(Status.DRIFTING, [link_diff])
    _sequence(monkeypatch, [round0])  # max_rounds=1: only one round is ever fetched
    _stub_dashboard(monkeypatch)
    monkeypatch.setattr(
        executor_module,
        "execute_link_actual_node",
        lambda client, action: LinkActualNodeResult(
            node_id=healthy.id, node_slug=healthy.slug, field="realized_device", candidate_id="dev-1"
        ),
    )

    envelope = run_reconcile(cfg, apply_changes=True, max_rounds=1)

    assert envelope.data.state == "manual_intervention_required"
    assert not any(e.code == "max_rounds_reached" for e in envelope.errors)
    assert len(envelope.data.rounds) == 1
    assert envelope.data.progress_made is True


def test_dry_plan_succeeds_despite_a_local_composition_error(tmp_path, monkeypatch):
    # Step 1.6 orchestration case 1: "one local composition error and no
    # actions -> dry plan succeeds". Plan mode never checks blocking
    # findings -- it always reports the plan for the operator to read.
    cfg = _config(tmp_path)
    _no_op_deployment_profiles(monkeypatch)
    node = _node("agblocked")
    diff = DiffRecord(
        target=Target(kind="node", slug=node.slug, name=node.name, id=node.id),
        code="unresolved_connection_path", severity=Severity.ERROR, message="x",
    )
    _sequence(monkeypatch, [_drift([_target_status(diff.target, Status.DRIFTING, [diff])])])

    envelope = run_reconcile(cfg, apply_changes=False)

    assert envelope.data.state == "planned"
    assert envelope.ok
    assert len(envelope.data.manual_review) == 1
    assert envelope.data.manual_review[0]["code"] == "unresolved_connection_path"
