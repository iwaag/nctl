from __future__ import annotations

import json
import subprocess
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
from nctl_core.sources.desired import (
    DesiredEndpoint,
    DesiredNode,
    DesiredNodeOperationalOverride,
    DesiredSnapshot,
)
from nctl_core.sources.snapshot import SourceSnapshot
from nctl_core.ssh_enroll import SshProbeRunner
from nctl_core.ssh_trust import build_ansible_ssh_common_args, derive_host_key_alias

# Every _node() below shares this fixed DesiredNode UUID regardless of slug.
NODE_ID = "11111111-1111-1111-1111-111111111111"
# The key _config() enrolls for NODE_ID and the default fake ssh_probe both offer.
FIXTURE_KEY_BLOB = "dGVzdC1yZWNvbmNpbGUtZml4dHVyZS1rZXktYnl0ZXM="


@pytest.fixture(autouse=True)
def _fake_ssh_probe(monkeypatch):
    """Default every test to a fake ssh-keyscan offering FIXTURE_KEY_BLOB.

    Without this, the fix_sshkey Step 5 round-start/post-regen scans would
    shell out to a real `ssh-keyscan` against fixture hostnames that don't
    exist. Tests that want to exercise a mismatch/unreachable scan result
    inject their own probe via run_reconcile(ssh_probe=...) instead.
    """

    def keyscan(host: str, port: int, timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["ssh-keyscan"], returncode=0, stdout=f"{host} ssh-ed25519 {FIXTURE_KEY_BLOB}\n", stderr=""
        )

    fake = SshProbeRunner(
        keyscan=keyscan,
        effective_config=lambda host, port: subprocess.CompletedProcess([], 0, "", ""),
        keygen_find=lambda path, host: subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(executor_module, "default_ssh_probe_runner", lambda: fake)


def _config(tmp_path):
    ansible_dir = tmp_path / "ansible_agdev"
    (ansible_dir / "playbooks").mkdir(parents=True)
    inventory = ansible_dir / "inventories/generated/hosts_intent.yml"
    inventory.parent.mkdir(parents=True)
    inventory.write_text("all: {}\n")
    config_path = tmp_path / "nctl.toml"
    known_hosts_file = tmp_path / "ssh" / "known_hosts"
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

[ssh]
known_hosts_file = "{known_hosts_file}"
lock_path = "{tmp_path / 'ssh.lock'}"
"""
    )
    cfg = Config.load(config_path)
    # Every reconcile-executor test fixture's node shares NODE_ID; enroll it by
    # default so existing tests exercise post-enrollment behavior, not the new
    # fix_sshkey Step 5 gate. Tests for the gate itself enroll a *different*
    # node explicitly instead of reusing this default.
    known_hosts_file.parent.mkdir(parents=True, exist_ok=True)
    alias = derive_host_key_alias(NODE_ID)
    known_hosts_file.write_text(f"{alias} ssh-ed25519 {FIXTURE_KEY_BLOB} nctl:test\n")
    return cfg


def _snapshot(nodes=()) -> SourceSnapshot:
    nodes = list(nodes)
    endpoints = [
        DesiredEndpoint(
            id=f"endpoint-{node.slug}", name="primary", endpoint_type="primary",
            node_id=node.id, node_slug=node.slug, mdns_name=f"{node.slug}.local",
        )
        for node in nodes
    ]
    return SourceSnapshot(
        desired=DesiredSnapshot(nodes=nodes, endpoints=endpoints),
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


def _drift(targets, *, generated_at="2026-07-17T00:00:00+00:00", nodes=None) -> tuple[SourceSnapshot, DriftResult, str]:
    snapshot = _snapshot(nodes=nodes if nodes is not None else [_node()])
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


def _resolved_ssh_targets_for_snapshot(snapshot, generation_id, generated_at):
    """Mirror `compose_production_inventory`'s ResolvedSshTarget derivation for a stubbed render.

    fix_sshkey3 Step 2: production-mode SSH scanning now reads only
    `ProductionRenderContext.ssh_targets`, never a route re-resolved from the
    snapshot at scan time -- so a test stubbing `build_production_render_context`
    (to skip the real, heavier composer) must still populate this map using
    the same pure route-resolution pipeline the real composer uses, or every
    service-phase scan target would come back `no_resolvable_production_target`.
    """
    from nctl_core.production.adapter import build_production_node_inputs
    from nctl_core.production.composer import ContractError, ResolvedSshTarget, resolve_effective_route, try_resolve_operational_values

    targets = {}
    for node in build_production_node_inputs(snapshot):
        effective, finding = try_resolve_operational_values(node, generated_at)
        if finding is not None or effective is None:
            continue
        try:
            connection = resolve_effective_route(node, effective)
        except ContractError:
            continue
        route = connection.get("ansible_host")
        if not route:
            continue
        targets[node.slug] = ResolvedSshTarget(
            slug=node.slug,
            desired_node_id=node.id,
            alias=derive_host_key_alias(node.id),
            route=route,
            port=effective.ansible_port.value if effective.ansible_port.value is not None else 22,
            generation_id=generation_id,
        )
    return targets


def _patch_production_render(monkeypatch, snapshot_factory, *, generated_at="2026-07-17T00:00:00+00:00"):
    """Stub `_regenerate_production_inventory`'s own fresh-snapshot fetch and compose call.

    fix_sshkey2 Step 3: `_regenerate_production_inventory` now fetches its own
    `SourceSnapshot` (rather than reusing the round's) and composes via
    `build_production_render_context`, so both must be stubbed together --
    stubbing only the old, now-unused `build_production_render` name would
    silently skip the real code path.
    """
    from nctl_core.production_render import ProductionRenderContext, ProductionRenderData

    monkeypatch.setattr(executor_module, "build_source_snapshot", lambda cfg, client: snapshot_factory())
    monkeypatch.setattr(
        executor_module,
        "build_production_render_context",
        lambda cfg, snapshot: ProductionRenderContext(
            envelope=Envelope.build("nctl.render.production.v1", ProductionRenderData(), []),
            generation_id="test-generation",
            generated_at=generated_at,
            source_snapshot=snapshot,
            ssh_targets=_resolved_ssh_targets_for_snapshot(snapshot, "test-generation", generated_at),
        ),
    )


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
    assert payload["schema"] == "nctl.reconcile.v2"
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


# --- SSH preflight (fix_sshkey Step 5) ---------------------------------------

UNENROLLED_NODE_ID = "99999999-9999-9999-9999-999999999999"


def _unenrolled_node(slug="agunenrolled") -> DesiredNode:
    return DesiredNode(
        id=UNENROLLED_NODE_ID, slug=slug, name=slug, lifecycle="active", node_type="device",
        accepted_actual_types=["device"],
    )


def test_apply_blocks_on_unenrolled_ssh_host_before_any_action_executes(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _no_op_deployment_profiles(monkeypatch)
    node = _unenrolled_node()
    diff = DiffRecord(target=Target(kind="node", slug=node.slug, name=node.name, id=node.id), code="missing_actual_data", severity=Severity.ERROR, message="x")
    _sequence(monkeypatch, [_drift([_target_status(diff.target, Status.UNKNOWN, [diff])], nodes=[node])])
    _stub_dashboard(monkeypatch)
    observation_calls = {"n": 0}
    monkeypatch.setattr(
        executor_module,
        "run_observation",
        lambda *a, **k: observation_calls.__setitem__("n", observation_calls["n"] + 1)
        or executor_module.ObservationResult(ok=True, hosts=[], collection=_fake_ansible_result(), retrieval=_fake_ansible_result()),
    )

    envelope = run_reconcile(cfg, apply_changes=True)

    assert not envelope.ok
    assert any(e.code == "ssh_host_key_unenrolled" for e in envelope.errors)
    assert observation_calls["n"] == 0
    assert envelope.data.rounds == []
    assert envelope.data.ssh_preflight == [
        {
            "slug": "agunenrolled", "alias": derive_host_key_alias(UNENROLLED_NODE_ID), "status": "unenrolled",
            "detail": "", "phase": "", "round": None, "route": "", "port": None, "generation_id": "",
            "managed_fingerprints": [], "offered_fingerprints": [],
        }
    ]


def test_dry_plan_reports_ssh_preflight_without_blocking(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _no_op_deployment_profiles(monkeypatch)
    node = _unenrolled_node()
    diff = DiffRecord(target=Target(kind="node", slug=node.slug, name=node.name, id=node.id), code="missing_actual_data", severity=Severity.ERROR, message="x")
    _sequence(monkeypatch, [_drift([_target_status(diff.target, Status.UNKNOWN, [diff])], nodes=[node])])

    envelope = run_reconcile(cfg, apply_changes=False)

    assert envelope.ok
    assert envelope.data.state == "planned"
    assert envelope.data.ssh_preflight[0]["status"] == "unenrolled"
    assert envelope.data.ssh_preflight[0]["slug"] == "agunenrolled"


def test_ledger_only_plan_not_blocked_by_unenrolled_host(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _no_op_deployment_profiles(monkeypatch)
    node = _unenrolled_node()
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
    called = {"n": 0}
    monkeypatch.setattr(
        executor_module,
        "execute_link_actual_node",
        lambda client, action: called.__setitem__("n", called["n"] + 1)
        or LinkActualNodeResult(node_id=node.id, node_slug=node.slug, field="realized_device", candidate_id="dev-1"),
    )

    envelope = run_reconcile(cfg, apply_changes=True)

    assert called["n"] == 1
    assert envelope.data.ssh_preflight == []


def test_apply_blocks_on_mismatched_offered_key_before_observation_runs(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _no_op_deployment_profiles(monkeypatch)
    node = _node()  # enrolled by _config() with FIXTURE_KEY_BLOB
    diff = DiffRecord(target=Target(kind="node", slug=node.slug, name=node.name, id=node.id), code="missing_actual_data", severity=Severity.ERROR, message="x")
    _sequence(monkeypatch, [_drift([_target_status(diff.target, Status.UNKNOWN, [diff])])])
    _stub_dashboard(monkeypatch)
    observation_calls = {"n": 0}
    monkeypatch.setattr(
        executor_module,
        "run_observation",
        lambda *a, **k: observation_calls.__setitem__("n", observation_calls["n"] + 1)
        or executor_module.ObservationResult(ok=True, hosts=[], collection=_fake_ansible_result(), retrieval=_fake_ansible_result()),
    )

    def keyscan(host, port, timeout):
        import subprocess as sp

        return sp.CompletedProcess(args=["ssh-keyscan"], returncode=0, stdout=f"{host} ssh-ed25519 {OTHER_KEY_BLOB}\n", stderr="")

    bad_probe = SshProbeRunner(keyscan=keyscan, effective_config=lambda h, p: subprocess.CompletedProcess([], 0, "", ""), keygen_find=lambda p, h: subprocess.CompletedProcess([], 0, "", ""))

    envelope = run_reconcile(cfg, apply_changes=True, ssh_probe=bad_probe)

    assert not envelope.ok
    assert any(e.code == "ssh_host_key_mismatch" for e in envelope.errors)
    assert observation_calls["n"] == 0
    assert envelope.data.rounds == []


OTHER_KEY_BLOB = "dGVzdC1yZWNvbmNpbGUtb3RoZXIta2V5LWJ5dGVz"


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
    _sequence(monkeypatch, [_drift([_target_status(diff.target, Status.UNKNOWN, [diff])], nodes=[node])])
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


# --- post-regeneration SSH route verification (fix_sshkey Step 5) -----------


def test_service_phase_blocks_on_mismatched_key_after_production_regen(tmp_path, monkeypatch):
    cfg = _config(tmp_path)

    def fake_load_profiles(playbook_dir):
        return ({"good": {"group": "good_server", "config_schema_version": "1", "variables": {}}}, "digest")

    monkeypatch.setattr(executor_module, "load_deployment_profiles", fake_load_profiles)

    from nctl_core.reconcile.profiles import ProfileAction, ProfileReconciliation

    monkeypatch.setattr(
        executor_module,
        "load_profile_reconciliation",
        lambda playbook_dir, names: {
            "good": ProfileReconciliation(action=ProfileAction(kind="playbook", playbook="playbooks/good.yml")),
        },
    )
    monkeypatch.setattr(executor_module, "write_production_artifacts", lambda envelope, out_dir: None)
    _stub_dashboard(monkeypatch)

    playbook_run_calls = {"n": 0}
    monkeypatch.setattr(
        executor_module,
        "AnsibleRunner",
        lambda *a, **k: SimpleNamespace(run=lambda *a2, **k2: playbook_run_calls.__setitem__("n", playbook_run_calls["n"] + 1)),
    )

    node = _node()
    good_service = _service_and_placement("good-svc", "good", node)
    diff = DiffRecord(
        target=Target(kind="service", slug="good-svc", name="good-svc", id="s-good"),
        code="service_not_running",
        severity=Severity.ERROR,
        message="x",
    )

    def make_snapshot():
        snapshot = _snapshot(nodes=[node])
        snapshot.desired.services = [good_service[0]]
        snapshot.desired.placements = [good_service[1]]
        # fix_sshkey2 Step 3: a resolvable production route is required for
        # this test to genuinely exercise route-based mismatch detection
        # (rather than an unrelated no_resolvable_production_route). "declared"
        # policy needs no realized/actual facts on this node fixture.
        snapshot.desired.operational_overrides = [
            DesiredNodeOperationalOverride(id="ov-1", node_id=node.id, declared_host_os="haos")
        ]
        return snapshot

    _patch_production_render(monkeypatch, make_snapshot)
    # fix_sshkey3 Step 2 item 7: the successful production regeneration
    # below means this failed round had a side effect, so the executor
    # performs one extra read-only drift refresh for the final drift --
    # a second fetch_and_compute_drift() call this fixture must supply.
    _sequence(
        monkeypatch,
        [
            (make_snapshot(), DriftResult(summary={"drifting": 1}, targets=[_target_status(diff.target, Status.DRIFTING, [diff])]), "2026-07-17T00:00:00+00:00"),
            (make_snapshot(), DriftResult(summary={"drifting": 1}, targets=[_target_status(diff.target, Status.DRIFTING, [diff])]), "2026-07-17T00:00:05+00:00"),
        ],
    )

    def keyscan(host, port, timeout):
        return subprocess.CompletedProcess(args=["ssh-keyscan"], returncode=0, stdout=f"{host} ssh-ed25519 {OTHER_KEY_BLOB}\n", stderr="")

    bad_probe = SshProbeRunner(keyscan=keyscan, effective_config=lambda h, p: subprocess.CompletedProcess([], 0, "", ""), keygen_find=lambda p, h: subprocess.CompletedProcess([], 0, "", ""))

    envelope = run_reconcile(cfg, apply_changes=True, ssh_probe=bad_probe)

    assert not envelope.ok
    assert any(e.code == "ssh_host_key_mismatch" for e in envelope.errors)
    assert playbook_run_calls["n"] == 0
    # fix_sshkey3 Step 2 item 6: the production regeneration that ran
    # successfully just before the mismatch was found must not be discarded
    # -- exactly one round is retained, with the regeneration's success and
    # no service_profile action (it never started).
    assert len(envelope.data.rounds) == 1
    round_reconciler_ids = [a.reconciler_id for a in envelope.data.rounds[0].actions]
    assert round_reconciler_ids == ["production_inventory"]
    assert envelope.data.rounds[0].actions[0].success is True
    # Item 7: a successful mutation happened (the regeneration), so this
    # counts as progress and a fresh final drift was fetched -- not the
    # pre-mutation drift from the top of this same round.
    assert envelope.data.progress_made is True
    assert envelope.data.final_drift_path


def test_production_write_failure_starts_no_service_ansible_process(tmp_path, monkeypatch):
    # fix_sshkey2 Step 3 required regression test: a production write or
    # validation failure starts no service Ansible process.
    cfg = _config(tmp_path)

    def fake_load_profiles(playbook_dir):
        return ({"good": {"group": "good_server", "config_schema_version": "1", "variables": {}}}, "digest")

    monkeypatch.setattr(executor_module, "load_deployment_profiles", fake_load_profiles)

    from nctl_core.reconcile.profiles import ProfileAction, ProfileReconciliation

    monkeypatch.setattr(
        executor_module,
        "load_profile_reconciliation",
        lambda playbook_dir, names: {
            "good": ProfileReconciliation(action=ProfileAction(kind="playbook", playbook="playbooks/good.yml")),
        },
    )
    monkeypatch.setattr(
        executor_module,
        "write_production_artifacts",
        lambda envelope, out_dir: EnvelopeError(code="inventory_validation_failed", message="ansible-inventory rejected the staged copy"),
    )
    _stub_dashboard(monkeypatch)

    playbook_run_calls = {"n": 0}
    monkeypatch.setattr(
        executor_module,
        "AnsibleRunner",
        lambda *a, **k: SimpleNamespace(run=lambda *a2, **k2: playbook_run_calls.__setitem__("n", playbook_run_calls["n"] + 1)),
    )

    node = _node()
    good_service = _service_and_placement("good-svc", "good", node)
    diff = DiffRecord(
        target=Target(kind="service", slug="good-svc", name="good-svc", id="s-good"),
        code="service_not_running",
        severity=Severity.ERROR,
        message="x",
    )

    def make_snapshot():
        snapshot = _snapshot(nodes=[node])
        snapshot.desired.services = [good_service[0]]
        snapshot.desired.placements = [good_service[1]]
        snapshot.desired.operational_overrides = [
            DesiredNodeOperationalOverride(id="ov-1", node_id=node.id, declared_host_os="haos")
        ]
        return snapshot

    _patch_production_render(monkeypatch, make_snapshot)
    _sequence(monkeypatch, [(make_snapshot(), DriftResult(summary={"drifting": 1}, targets=[_target_status(diff.target, Status.DRIFTING, [diff])]), "2026-07-17T00:00:00+00:00")])

    envelope = run_reconcile(cfg, apply_changes=True)

    assert not envelope.ok
    assert any(e.code == "production_regeneration_unavailable" for e in envelope.errors)
    assert playbook_run_calls["n"] == 0
    # fix_sshkey3 Step 2 item 6: the round is still retained (with the
    # failed regeneration action) rather than discarded -- it had no side
    # effects (the regeneration itself failed), so no extra drift refresh.
    assert len(envelope.data.rounds) == 1
    assert [a.reconciler_id for a in envelope.data.rounds[0].actions] == ["production_inventory"]
    assert envelope.data.rounds[0].actions[0].success is False


def test_service_phase_scans_freshly_regenerated_route_not_round_start_snapshot(tmp_path, monkeypatch):
    # fix_sshkey2 Step 3 required regression test (bug #2): the round-start
    # snapshot contains an old IP, while an IPAM-style update means the
    # snapshot _regenerate_production_inventory freshly fetches contains a
    # new IP. keyscan must be called only against the new IP.
    cfg = _config(tmp_path)

    def fake_load_profiles(playbook_dir):
        return ({"good": {"group": "good_server", "config_schema_version": "1", "variables": {}}}, "digest")

    monkeypatch.setattr(executor_module, "load_deployment_profiles", fake_load_profiles)

    from nctl_core.reconcile.profiles import ProfileAction, ProfileReconciliation

    monkeypatch.setattr(
        executor_module,
        "load_profile_reconciliation",
        lambda playbook_dir, names: {
            "good": ProfileReconciliation(action=ProfileAction(kind="playbook", playbook="playbooks/good.yml")),
        },
    )
    monkeypatch.setattr(executor_module, "write_production_artifacts", lambda envelope, out_dir: None)
    _stub_dashboard(monkeypatch)

    playbook_run_calls = {"n": 0}

    def fake_runner_run(self, args, *, mode, artifact_stem=None):
        from nctl_core.ansible import AnsibleRunResult

        playbook_run_calls["n"] += 1
        return AnsibleRunResult(mode=mode, command=args, exit_code=0)

    monkeypatch.setattr(executor_module.AnsibleRunner, "run", fake_runner_run)

    node = _node()
    good_service = _service_and_placement("good-svc", "good", node)
    diff = DiffRecord(
        target=Target(kind="service", slug="good-svc", name="good-svc", id="s-good"),
        code="service_not_running",
        severity=Severity.ERROR,
        message="x",
    )

    def snapshot_with_ip(ip: str):
        snapshot = _snapshot(nodes=[node])
        snapshot.desired.endpoints[0].ip_address = ip
        snapshot.desired.services = [good_service[0]]
        snapshot.desired.placements = [good_service[1]]
        snapshot.desired.operational_overrides = [
            DesiredNodeOperationalOverride(id="ov-1", node_id=node.id, declared_host_os="haos")
        ]
        return snapshot

    OLD_IP = "10.0.0.1"
    NEW_IP = "10.0.0.2"
    _patch_production_render(monkeypatch, lambda: snapshot_with_ip(NEW_IP))
    _sequence(
        monkeypatch,
        [
            (snapshot_with_ip(OLD_IP), DriftResult(summary={"drifting": 1}, targets=[_target_status(diff.target, Status.DRIFTING, [diff])]), "2026-07-17T00:00:00+00:00"),
            (snapshot_with_ip(NEW_IP), DriftResult(summary={}, targets=[_target_status(diff.target, Status.CONVERGED, [])]), "2026-07-17T00:05:00+00:00"),
        ],
    )

    scanned_hosts = []

    def keyscan(host, port, timeout):
        scanned_hosts.append(host)
        return subprocess.CompletedProcess(args=["ssh-keyscan"], returncode=0, stdout=f"{host} ssh-ed25519 {FIXTURE_KEY_BLOB}\n", stderr="")

    probe = SshProbeRunner(
        keyscan=keyscan,
        effective_config=lambda h, p: subprocess.CompletedProcess([], 0, "", ""),
        keygen_find=lambda p, h: subprocess.CompletedProcess([], 0, "", ""),
    )

    envelope = run_reconcile(cfg, apply_changes=True, ssh_probe=probe)

    assert envelope.ok, envelope.errors
    assert NEW_IP in scanned_hosts
    assert OLD_IP not in scanned_hosts
    # fix_sshkey3 Step 2 item 8: post-actuation observation now derives its
    # host list from parameters["host_slugs"] (the real node, "agweb"), not
    # the service action's own target slug ("good-svc") -- so it actually
    # runs (2 more AnsibleRunner calls: collect + retrieve) alongside the
    # one service_profile playbook run.
    observation_actions = [
        a for r in envelope.data.rounds for a in r.actions if a.reconciler_id == "observe_node"
    ]
    assert len(observation_actions) == 1
    assert observation_actions[0].target_slugs == ["agweb"]
    assert playbook_run_calls["n"] == 3
    # Item 7: the round's own production SSH scan evidence (route/port/
    # generation/fingerprints, no raw key blobs) is retained on the round,
    # not just as a flattened top-level enrollment summary.
    [preflight_entry] = envelope.data.rounds[0].ssh_preflight
    assert preflight_entry["route"] == NEW_IP
    assert preflight_entry["status"] == "ready"
    assert preflight_entry["phase"] == "production_route"
    assert preflight_entry["generation_id"] == "test-generation"
    assert preflight_entry["managed_fingerprints"] and preflight_entry["offered_fingerprints"]
    assert "key_blob" not in str(preflight_entry)


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
        # fix_sshkey2 Step 3: a resolvable production route is required so the
        # post-regen scan reports ready (matching FIXTURE_KEY_BLOB) instead of
        # no_resolvable_production_route.
        snapshot.desired.operational_overrides = [
            DesiredNodeOperationalOverride(id="ov-1", node_id=node.id, declared_host_os="haos")
        ]
        return snapshot

    _patch_production_render(monkeypatch, make_snapshot)

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


def test_interruption_mid_round_retains_actions_completed_before_it(tmp_path, monkeypatch):
    # fix_sshkey3 Step 2 (contract item 6): interruption is one of the
    # explicitly listed cases where `_execute_round`'s partial evidence must
    # still be appended to `data.rounds` -- not just regeneration/SSH-scan
    # failures.
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
    monkeypatch.setattr(executor_module, "write_production_artifacts", lambda envelope, out_dir: None)
    _stub_dashboard(monkeypatch)

    node = _node()
    good_service = _service_and_placement("good-svc", "good", node)
    bad_service = _service_and_placement("bad-svc", "bad", node)
    good_diff = DiffRecord(
        target=Target(kind="service", slug="good-svc", name="good-svc", id="s-good"),
        code="service_not_running", severity=Severity.ERROR, message="x",
    )
    bad_diff = DiffRecord(
        target=Target(kind="service", slug="bad-svc", name="bad-svc", id="s-bad"),
        code="service_not_running", severity=Severity.ERROR, message="x",
    )

    def make_snapshot():
        snapshot = _snapshot(nodes=[node])
        snapshot.desired.services = [good_service[0], bad_service[0]]
        snapshot.desired.placements = [good_service[1], bad_service[1]]
        snapshot.desired.operational_overrides = [
            DesiredNodeOperationalOverride(id="ov-1", node_id=node.id, declared_host_os="haos")
        ]
        return snapshot

    _patch_production_render(monkeypatch, make_snapshot)
    drift = (
        make_snapshot(),
        DriftResult(
            summary={"drifting": 2},
            targets=[
                _target_status(good_diff.target, Status.DRIFTING, [good_diff]),
                _target_status(bad_diff.target, Status.DRIFTING, [bad_diff]),
            ],
        ),
        "2026-07-17T00:00:00+00:00",
    )
    # One extra fetch for the post-interruption final-drift refresh (item 7):
    # the first service action below succeeds, so the round had a side effect.
    _sequence(monkeypatch, [drift, drift])

    class ToggleInterrupt:
        def __init__(self):
            self.triggered = False

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def is_set(self):
            return self.triggered

    flag = ToggleInterrupt()
    monkeypatch.setattr(executor_module, "_InterruptFlag", lambda: flag)

    def fake_runner_run(self, args, *, mode, artifact_stem=None):
        from nctl_core.ansible import AnsibleRunResult

        flag.triggered = True  # interrupt is discovered only after this action's playbook ran
        return AnsibleRunResult(mode=mode, command=args, exit_code=0)

    monkeypatch.setattr(executor_module.AnsibleRunner, "run", fake_runner_run)

    envelope = run_reconcile(cfg, apply_changes=True)

    assert not envelope.ok
    assert any(e.code == "interrupted" for e in envelope.errors)
    assert len(envelope.data.rounds) == 1
    round0_actions = envelope.data.rounds[0].actions
    assert any(a.reconciler_id == "production_inventory" and a.success for a in round0_actions)
    service_actions = [a for a in round0_actions if a.reconciler_id == "service_profile"]
    assert len(service_actions) == 1
    assert service_actions[0].success is True


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


def test_ssh_scan_errors_maps_unenrolled_status_too(tmp_path):
    # fix_sshkey3 Step 1 (contract item 6): a managed-store entry removed
    # between the round-start enrollment gate and this post-scan check must
    # stop the round rather than silently falling through to Ansible --
    # `_ssh_scan_errors` previously only recognized mismatch/unreachable.
    entries = [
        executor_module.SshPreflightEntry(slug="agdnsmasq", alias="nctl-node-x", status=executor_module.STATUS_UNENROLLED)
    ]
    errors = executor_module._ssh_scan_errors(entries)
    assert len(errors) == 1
    assert errors[0].code == "ssh_host_key_unenrolled"
    assert "agdnsmasq" in errors[0].message


def test_apply_reports_ssh_store_read_failed_when_managed_store_is_corrupt(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _no_op_deployment_profiles(monkeypatch)
    node = _unenrolled_node()
    diff = DiffRecord(target=Target(kind="node", slug=node.slug, name=node.name, id=node.id), code="missing_actual_data", severity=Severity.ERROR, message="x")
    _sequence(monkeypatch, [_drift([_target_status(diff.target, Status.UNKNOWN, [diff])], nodes=[node])])
    known_hosts_path = cfg.resolved_ssh_known_hosts_file()
    known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
    known_hosts_path.write_bytes(b"\xff\xfe\x00bad")

    envelope = run_reconcile(cfg, apply_changes=True)

    assert not envelope.ok
    assert any(e.code == "ssh_store_read_failed" for e in envelope.errors)
    assert envelope.data.rounds == []


# --- observation-time store failures are round-safe (fix_sshkey4 Step 2) -----


def _direct_round_setup(tmp_path, monkeypatch):
    """Build the plumbing to call `_execute_round` directly, bypassing the planner.

    Lets these tests construct exactly the bootstrap-action sequence they
    need (a successful ledger action followed by an observe_node action)
    without depending on drift/planner internals unrelated to this contract.
    """
    from pathlib import Path

    from nctl_core.artifacts import OperationArtifacts
    from nctl_core.events import OperationLog
    from nctl_core.reconcile.model import PlanScope, ReconcilePlan

    cfg = _config(tmp_path)
    op = OperationLog("reconcile", cfg.events.resolved_log_dir())
    artifacts = OperationArtifacts.create(cfg.events.resolved_log_dir(), op.operation_id)
    node = _node()
    snapshot = _snapshot(nodes=[node])

    class _NeverInterrupted:
        def is_set(self):
            return False

    dummy_probe = SshProbeRunner(
        keyscan=lambda host, port, timeout: subprocess.CompletedProcess([], 0, "", ""),
        effective_config=lambda host, port: subprocess.CompletedProcess([], 0, "", ""),
        keygen_find=lambda path, host: subprocess.CompletedProcess([], 0, "", ""),
    )

    def make_plan(actions):
        return ReconcilePlan(
            scope=PlanScope(kind="cluster"),
            drift_fingerprint="fp1",
            generated_at=datetime.now(timezone.utc),
            actions=actions,
        )

    return SimpleNamespace(
        cfg=cfg,
        op=op,
        artifacts=artifacts,
        node=node,
        snapshot=snapshot,
        interrupted=_NeverInterrupted(),
        probe=dummy_probe,
        make_plan=make_plan,
        Path=Path,
    )


def test_successful_ledger_action_retained_when_observation_store_fails(tmp_path, monkeypatch):
    # Corrected contract 2, second boundary: a ledger action that already
    # succeeded this round must stay in the round's evidence even though a
    # later bootstrap observe_node action in the same round hits a
    # managed-store failure.
    ctx = _direct_round_setup(tmp_path, monkeypatch)
    link_action = ReconcileAction(
        id="link-1",
        reconciler_id="link_actual_node",
        action_kind="ledger",
        targets=[Target(kind="node", slug=ctx.node.slug, name=ctx.node.name, id=ctx.node.id)],
        claimed_diff_codes=["actual_node_not_linked"],
        reason="test",
        mutates=True,
        requires_observation=False,
    )
    observe_action = ReconcileAction(
        id="observe-1",
        reconciler_id="observe_node",
        action_kind="observation",
        targets=[Target(kind="node", slug=ctx.node.slug, name=ctx.node.name, id=ctx.node.id)],
        claimed_diff_codes=["service_observation_missing"],
        reason="test",
        mutates=False,
        requires_observation=False,
    )
    plan = ctx.make_plan([link_action, observe_action])

    monkeypatch.setattr(
        executor_module,
        "execute_link_actual_node",
        lambda client, action: LinkActualNodeResult(
            node_id=ctx.node.id, node_slug=ctx.node.slug, field="realized_device", candidate_id="dev-1"
        ),
    )
    monkeypatch.setattr(
        executor_module,
        "run_observation",
        lambda *a, **kw: (_ for _ in ()).throw(executor_module.SshStoreReadError("store corrupted mid-round")),
    )

    outcome = executor_module._execute_round(
        ctx.cfg, ctx.op, ctx.artifacts, 0, plan, ctx.snapshot,
        lambda: datetime.now(timezone.utc), None, ctx.interrupted, ctx.probe,
    )

    assert len(outcome.summary.actions) == 2
    link_result, observe_result = outcome.summary.actions
    assert link_result.reconciler_id == "link_actual_node" and link_result.success is True
    assert observe_result.reconciler_id == "observe_node" and observe_result.success is False
    assert "ssh_store_read_failed" in observe_result.error
    assert outcome.had_side_effects is True
    assert len(outcome.terminal_errors) == 1
    assert outcome.terminal_errors[0].code == "ssh_store_read_failed"


def test_post_actuation_observation_store_failure_retains_deployment_evidence(tmp_path, monkeypatch):
    # Corrected contract 2, post-actuation boundary: a dnsmasq deployment
    # that already succeeded this round must stay in the round's evidence
    # even though the post-actuation observation that follows it hits a
    # managed-store failure.
    ctx = _direct_round_setup(tmp_path, monkeypatch)
    ctx.snapshot.desired.operational_overrides = [
        DesiredNodeOperationalOverride(id="ov-1", node_id=ctx.node.id, declared_host_os="haos")
    ]
    dnsmasq_action = ReconcileAction(
        id="dnsmasq-1",
        reconciler_id="dnsmasq_config",
        action_kind="dnsmasq_config",
        targets=[Target(kind="service", slug="dnsmasq", name="dnsmasq", id="svc-1")],
        claimed_diff_codes=["service_config_mismatch"],
        reason="test",
        mutates=True,
        requires_observation=True,
        parameters={"host_slugs": [ctx.node.slug]},
    )
    plan = ctx.make_plan([dnsmasq_action])

    from nctl_core.output import Envelope as _Envelope
    from nctl_core.dnsmasq_apply import DnsmasqApplyData

    def fake_load_profiles(playbook_dir):
        return ({"good": {"group": "good_server", "config_schema_version": "1", "variables": {}}}, "digest")

    monkeypatch.setattr(executor_module, "load_deployment_profiles", fake_load_profiles)
    monkeypatch.setattr(executor_module, "write_production_artifacts", lambda envelope, out_dir: None)
    _patch_production_render(monkeypatch, lambda: ctx.snapshot)
    monkeypatch.setattr(
        executor_module,
        "build_dnsmasq_apply",
        lambda cfg, apply_changes=False, probe=None, host_limit=None: _Envelope.build(
            "nctl.dnsmasq.apply.v2",
            DnsmasqApplyData(operation_id="op-1", mode="apply", event_log_path="events/op-1.jsonl"),
            [],
        ),
    )
    monkeypatch.setattr(
        executor_module,
        "run_observation",
        lambda *a, **kw: (_ for _ in ()).throw(executor_module.SshStoreReadError("store corrupted post-actuation")),
    )

    matching_probe = SshProbeRunner(
        keyscan=lambda host, port, timeout: subprocess.CompletedProcess(
            args=["ssh-keyscan"], returncode=0, stdout=f"{host} ssh-ed25519 {FIXTURE_KEY_BLOB}\n", stderr=""
        ),
        effective_config=lambda host, port: subprocess.CompletedProcess([], 0, "", ""),
        keygen_find=lambda path, host: subprocess.CompletedProcess([], 0, "", ""),
    )
    outcome = executor_module._execute_round(
        ctx.cfg, ctx.op, ctx.artifacts, 0, plan, ctx.snapshot,
        lambda: datetime.now(timezone.utc), None, ctx.interrupted, matching_probe,
    )

    reconciler_ids = [a.reconciler_id for a in outcome.summary.actions]
    assert "production_inventory" in reconciler_ids
    assert "dnsmasq_config" in reconciler_ids
    dnsmasq_result = next(a for a in outcome.summary.actions if a.reconciler_id == "dnsmasq_config")
    assert dnsmasq_result.success is True
    observe_result = outcome.summary.actions[-1]
    assert observe_result.reconciler_id == "observe_node" and observe_result.success is False
    assert outcome.had_side_effects is True
    assert len(outcome.terminal_errors) == 1
    assert outcome.terminal_errors[0].code == "ssh_store_read_failed"


def test_final_drift_refresh_failure_after_store_failure_reports_unknown(tmp_path, monkeypatch):
    # Item 7 (fix_sshkey3 Step 2, reused unchanged by fix_sshkey4 Step 2): a
    # terminal store failure with prior side effects triggers one final-drift
    # refresh; if that refresh itself fails, the run must report
    # `final_drift_unknown` rather than silently keeping the stale
    # pre-mutation drift. `_execute_round` is stubbed directly to a crafted
    # `RoundOutcome` so this exercises `_run_apply`'s generic handling of that
    # outcome without depending on planner action ordering.
    from nctl_core.sources.actual import ActualDevice, ActualSnapshot

    cfg = _config(tmp_path)
    _no_op_deployment_profiles(monkeypatch)
    node = _node()
    diff = DiffRecord(target=Target(kind="node", slug=node.slug, name=node.name, id=node.id), code="actual_node_not_linked", severity=Severity.ERROR, message="x")

    def make_drift():
        snapshot = _snapshot(nodes=[node])
        snapshot.actual = ActualSnapshot(devices=[ActualDevice(id="dev-1", name=node.name)])
        targets = [_target_status(diff.target, Status.DRIFTING, [diff])]
        return snapshot, DriftResult(summary={"drifting": 1}, targets=targets), "2026-07-17T00:00:00+00:00"

    _sequence(
        monkeypatch,
        [
            make_drift(),
            EnvelopeError(code="nautobot_fetch_failed", message="refresh failed"),
        ],
    )

    def fake_execute_round(cfg, op, artifacts, round_index, plan, snapshot, now, command_runner, interrupted, ssh_probe):
        summary = executor_module.RoundSummary(round=round_index, drift_fingerprint=plan.drift_fingerprint)
        summary.actions.append(
            executor_module.ActionResult(
                action_id="link-1", reconciler_id="link_actual_node", action_kind="ledger",
                target_slugs=[node.slug], success=True,
            )
        )
        return executor_module.RoundOutcome(
            summary=summary,
            terminal_errors=[EnvelopeError(code="ssh_store_read_failed", message="store corrupted post-actuation")],
            had_side_effects=True,
        )

    monkeypatch.setattr(executor_module, "_execute_round", fake_execute_round)

    envelope = run_reconcile(cfg, apply_changes=True)

    assert not envelope.ok
    assert any(e.code == "ssh_store_read_failed" for e in envelope.errors)
    assert any(e.code == "final_drift_unknown" for e in envelope.errors)
    assert len(envelope.data.rounds) == 1
    assert envelope.data.rounds[0].actions[0].success is True
    assert envelope.data.progress_made is True
    assert envelope.data.final_drift_path == ""


def test_pre_round_store_failure_still_starts_no_round(tmp_path, monkeypatch):
    # No successful mutation has happened yet at the pre-round enrollment
    # gate, so a store failure there must still report zero rounds and no
    # progress -- distinct from the mid-round/post-actuation cases above.
    cfg = _config(tmp_path)
    _no_op_deployment_profiles(monkeypatch)
    node = _node()
    diff = DiffRecord(target=Target(kind="node", slug=node.slug, name=node.name, id=node.id), code="missing_actual_data", severity=Severity.ERROR, message="x")
    _sequence(monkeypatch, [_drift([_target_status(diff.target, Status.UNKNOWN, [diff])], nodes=[node])])
    known_hosts_path = cfg.resolved_ssh_known_hosts_file()
    known_hosts_path.write_bytes(b"\xff\xfe\x00bad")

    envelope = run_reconcile(cfg, apply_changes=True)

    assert not envelope.ok
    assert any(e.code == "ssh_store_read_failed" for e in envelope.errors)
    assert envelope.data.rounds == []
    assert envelope.data.progress_made is False


# --- real multi-round dnsmasq content convergence (fix_sshkey4 Step 5) -------
#
# Unlike every test above (which stubs fetch_and_compute_drift itself with a
# hand-built DriftResult), this test mocks only the true external boundary --
# nctl_core.sources.snapshot.build_source_snapshot, i.e. the Nautobot fetch --
# and lets the real drift engine (compute_drift/evaluate_all_services/
# service_placement content-drift), classify(), and the planner run
# unmodified against a synthetic multi-round SourceSnapshot. Ansible/SSH stay
# mocked at their existing subprocess/probe boundaries.


def test_real_multi_round_dnsmasq_content_convergence(tmp_path, monkeypatch):
    from nctl_core.ansible import AnsibleRunResult
    from nctl_core.dnsmasq_render import compute_dnsmasq_render

    cfg = _config(tmp_path)
    ansible_dir = cfg.ansible.resolved_playbook_dir(cfg.source_path.parent)
    (ansible_dir / "playbooks" / "bootstrap").mkdir(parents=True, exist_ok=True)
    (ansible_dir / "playbooks" / "bootstrap" / "setup_dnsmasq.yml").write_text("---\n")
    (ansible_dir / "playbooks" / "dnsmasq").mkdir(parents=True, exist_ok=True)
    (ansible_dir / "playbooks" / "dnsmasq" / "deploy_dnsmasq_records.yml").write_text("---\n")
    records_path = "/etc/dnsmasq.d/nintent-records.conf"
    (ansible_dir / "vars").mkdir(parents=True, exist_ok=True)
    (ansible_dir / "vars" / "deployment_profiles.yml").write_text(
        """
deployment_profiles:
  dnsmasq:
    group: dnsmasq_server
    config_schema_version: "1"
    variables: {}
deployment_profile_reconciliation:
  dnsmasq:
    action:
      kind: dnsmasq_config
      managed_files:
        records:
          path: %s
          digest: sha256
"""
        % records_path
    )

    node = _node("agdnsmasq").model_copy(update={"realized_device_id": "dev-1"})
    service, placement = _service_and_placement("dnsmasq", "dnsmasq", node)
    route = "192.0.2.50"

    base_snapshot = _snapshot(nodes=[node])
    desired_digest = compute_dnsmasq_render(base_snapshot).content_sha256
    old_digest = "0" * 64
    assert old_digest != desired_digest

    def make_snapshot(*, sha256: str, status: str = "present") -> SourceSnapshot:
        snapshot = _snapshot(nodes=[node])
        snapshot.desired.services = [service]
        snapshot.desired.placements = [placement]
        snapshot.actual = ActualSnapshot(
            devices=[
                ActualDevice(
                    id="dev-1",
                    name="agdnsmasq",
                    facts={
                        "host_system": "Linux",
                        "primary_ip_address": route,
                        "last_seen": datetime.now(timezone.utc).isoformat(),
                        "service_inventory_updated_at": datetime.now(timezone.utc).isoformat(),
                        "observed_services": {
                            "dnsmasq": {
                                "state": "running",
                                "source": "systemd",
                                "checked_at": datetime.now(timezone.utc).isoformat(),
                                "managed_files": {
                                    "records": {"path": records_path, "status": status, "sha256": sha256}
                                },
                            }
                        },
                    },
                )
            ]
        )
        return snapshot

    call_count = {"n": 0}

    def fetch_snapshot(cfg, client):
        call_count["n"] += 1
        # Round 0's own drift fetch and its subsequent production-regeneration
        # fetch (calls 1-2) both see the stale digest; a real v2
        # observation/ingest would update this before round 1's fresh fetch
        # (call 3+) -- represented here since the round loop always re-fetches
        # drift from the top rather than trusting its own action's result.
        digest = old_digest if call_count["n"] <= 2 else desired_digest
        return make_snapshot(sha256=digest)

    monkeypatch.setattr("nctl_core.drift_render.build_source_snapshot", fetch_snapshot)
    # build_dnsmasq_apply -> build_dnsmasq_render makes its own separate
    # SourceSnapshot fetch (a different Nautobot round-trip from the
    # drift/regen fetches above) -- it only reads desired data for rendering,
    # which never changes across rounds, but share the same round-aware fake
    # for consistency.
    monkeypatch.setattr("nctl_core.dnsmasq_render.build_source_snapshot", fetch_snapshot)
    _patch_production_render(monkeypatch, lambda: fetch_snapshot(cfg, None))
    monkeypatch.setattr(executor_module, "write_production_artifacts", lambda envelope, out_dir: None)
    monkeypatch.setattr(executor_module, "run_observation", lambda *a, **kw: executor_module.ObservationResult(ok=True, hosts=[], collection=_fake_ansible_result(), retrieval=_fake_ansible_result()))
    _stub_dashboard(monkeypatch)

    inventory_payload = {
        "dnsmasq_server": {"hosts": ["agdnsmasq"]},
        "_meta": {
            "hostvars": {
                "agdnsmasq": {
                    "nintent_desired_node_id": NODE_ID,
                    "nctl_ssh_host_key_alias": derive_host_key_alias(NODE_ID),
                    "ansible_ssh_common_args": build_ansible_ssh_common_args(
                        derive_host_key_alias(NODE_ID), str(cfg.resolved_ssh_known_hosts_file())
                    ),
                    "ansible_host": route,
                }
            }
        },
    }
    ansible_calls = []

    def fake_runner_run(self, args, *, mode, artifact_stem=None):
        ansible_calls.append(args)
        if args[0] == "ansible-inventory":
            return AnsibleRunResult(mode=mode, command=args, exit_code=0, stdout=json.dumps(inventory_payload))
        return AnsibleRunResult(mode=mode, command=args, exit_code=0, stdout="agdnsmasq : ok=1 changed=0 unreachable=0 failed=0\n")

    monkeypatch.setattr(executor_module.AnsibleRunner, "run", fake_runner_run)

    # Round 0: content mismatch -> service_config_mismatch -> exact production
    # SSH preflight -> dnsmasq_config deploy succeeds -> post-actuation
    # observation (mocked at its own existing boundary; the real drift refetch
    # at the top of round 1 is what actually proves convergence below).
    round0 = run_reconcile(cfg, apply_changes=True, max_rounds=1)
    # max_rounds=1 deliberately stops right after this one round's actuation
    # (state=max_rounds_reached, not converged) so the second run_reconcile
    # call below observes a fresh top-of-round fetch, not a second round
    # inside the same call -- the action results below are what this test
    # actually verifies.
    assert len(round0.data.rounds) == 1
    round0_actions = {a.reconciler_id: a for a in round0.data.rounds[0].actions}
    assert round0_actions["dnsmasq_config"].success is True
    assert round0_actions["dnsmasq_config"].target_slugs == ["dnsmasq"]  # the service target; host is in parameters
    assert round0_actions["observe_node"].success is True
    [preflight_entry] = round0.data.rounds[0].ssh_preflight
    assert preflight_entry["route"] == route
    assert preflight_entry["phase"] == "production_route"
    assert preflight_entry["status"] == "ready"
    assert preflight_entry["managed_fingerprints"] and preflight_entry["offered_fingerprints"]
    deploy_call = next(
        c for c in ansible_calls if c[0] == "ansible-playbook" and any("deploy_dnsmasq_records.yml" in str(a) for a in c)
    )
    extra_vars = json.loads(deploy_call[deploy_call.index("-e") + 1])
    assert extra_vars["dnsmasq_records_config_file"] == records_path

    # Round 1: real drift recomputed from the (now matching) fresh fetch ->
    # no service_config_* diff -> no repeated dnsmasq_config action ->
    # converged for the dnsmasq service scope.
    ansible_calls.clear()
    round1 = run_reconcile(cfg, apply_changes=True, max_rounds=1)
    assert round1.ok, round1.errors
    assert round1.data.state == "already_converged"
    assert round1.data.rounds == []
    assert not any(
        c[0] == "ansible-playbook" and any("deploy_dnsmasq_records.yml" in str(a) for a in c) for c in ansible_calls
    )
