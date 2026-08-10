from __future__ import annotations

from datetime import datetime, timezone

import pytest

from nctl_core.drift.model import DiffRecord, Severity, Target
from nctl_core.drift.evaluation_snapshot import evaluate_all_nodes
from nctl_core.reconcile.classify import UnclassifiedDiffCodeError, classify
from nctl_core.reconcile.fingerprint import compute_drift_fingerprint
from nctl_core.reconcile.planner import HostScopeError, build_plan, select_scoped_diffs
from nctl_core.reconcile import planner as planner_module
from nctl_core.reconcile.model import Classification, PlanScope
from nctl_core.reconcile.profiles import ProfileAction, ProfileReconciliation
from nctl_core.reconcile.ssh_preflight import ssh_required_host_slugs
from nctl_core.sources.actual import ActualCluster, ActualDevice, ActualSnapshot, ActualVirtualMachine
from nctl_core.sources.desired import (
    DesiredComputeInstance,
    DesiredComputePlatform,
    DesiredNode,
    DesiredService,
    DesiredServicePlacement,
    DesiredSnapshot,
)
from nctl_core.sources.snapshot import SourceSnapshot


def _node(
    node_id: str,
    slug: str,
    *,
    realized_device_id: str | None = None,
    accepted_actual_types: list[str] | None = None,
) -> DesiredNode:
    return DesiredNode(
        id=node_id,
        slug=slug,
        name=slug,
        lifecycle="active",
        node_type="device",
        accepted_actual_types=accepted_actual_types or ["device"],
        realized_device_id=realized_device_id,
    )


def _service(service_id: str, slug: str) -> DesiredService:
    return DesiredService(
        id=service_id,
        slug=slug,
        name=slug,
        display_name=slug,
        lifecycle="active",
    )


def _placement(
    placement_id: str, *, service_id: str, node_id: str, deployment_profile: str
) -> DesiredServicePlacement:
    return DesiredServicePlacement(
        id=placement_id,
        service_id=service_id,
        node_id=node_id,
        instance_name=f"{deployment_profile}-{node_id}",
        deployment_profile=deployment_profile,
        config_schema_version="1",
    )


def _snapshot(*, nodes=(), devices=(), virtual_machines=(), services=(), placements=()) -> SourceSnapshot:
    return SourceSnapshot(
        desired=DesiredSnapshot(nodes=list(nodes), services=list(services), placements=list(placements)),
        actual=ActualSnapshot(devices=list(devices), virtual_machines=list(virtual_machines)),
        fetched_at=datetime.now(timezone.utc),
    )


def _node_diff(node: DesiredNode, code: str, severity: Severity = Severity.ERROR) -> DiffRecord:
    return DiffRecord(
        target=Target(kind="node", slug=node.slug, name=node.name, id=node.id),
        code=code,
        severity=severity,
        message=f"{node.slug}: {code}",
    )


def _service_diff(service: DesiredService, code: str, severity: Severity = Severity.ERROR) -> DiffRecord:
    return DiffRecord(
        target=Target(kind="service", slug=service.slug, name=service.name, id=service.id),
        code=code,
        severity=severity,
        message=f"{service.slug}: {code}",
    )


def _global_diff(code: str) -> DiffRecord:
    return DiffRecord(target=Target(kind="global"), code=code, severity=Severity.ERROR, message=code)


def _service_observation_diff(
    service: DesiredService, node: DesiredNode, code: str = "service_observation_missing"
) -> DiffRecord:
    return DiffRecord(
        target=Target(kind="service", slug=service.slug, name=service.name, id=service.id),
        code=code,
        severity=Severity.ERROR,
        message=f"{service.slug}: {code}",
        desired={"expected": {"node_slug": node.slug, "node_id": node.id}},
    )


CLUSTER = PlanScope(kind="cluster")


def _build(snapshot, diffs, scope=CLUSTER, profile_reconciliation=None):
    return build_plan(
        snapshot=snapshot,
        diffs=diffs,
        scope=scope,
        drift_generated_at="2026-07-17T00:00:00+00:00",
        profile_reconciliation=profile_reconciliation or {},
    )


# --- scope selection -------------------------------------------------------


def test_select_scoped_diffs_host_scope_filters_correctly():
    web = _node("n1", "agweb")
    db = _node("n2", "agdb")
    svc = _service("s1", "nginx")
    placement = _placement("p1", service_id="s1", node_id="n1", deployment_profile="nginx")
    snapshot = _snapshot(nodes=[web, db], services=[svc], placements=[placement])

    diffs = [
        _global_diff("unknown_profile"),
        _node_diff(web, "actual_node_not_linked"),
        _node_diff(db, "actual_node_not_linked"),
        _service_diff(svc, "service_not_running"),
    ]

    scoped = select_scoped_diffs(diffs, PlanScope(kind="host", host_slug="agweb"), snapshot)

    codes_by_target = {(d.target.kind, d.target.slug): d.code for d in scoped}
    assert ("global", None) in codes_by_target
    assert ("node", "agweb") in codes_by_target
    assert ("node", "agdb") not in codes_by_target
    assert ("service", "nginx") in codes_by_target  # placed on agweb


def test_select_scoped_diffs_unknown_host_raises():
    snapshot = _snapshot(nodes=[_node("n1", "agweb")])
    with pytest.raises(HostScopeError):
        select_scoped_diffs([], PlanScope(kind="host", host_slug="ghost"), snapshot)


def test_select_scoped_diffs_placement_specific_observation_stays_on_its_owning_node():
    """The p3/fix1 regression shape: a node_agent-style service placed on three
    hosts must not widen a host-scoped `--refresh-observation` beyond the one
    requested host merely because the service also runs there."""
    aghub = _node("n1", "aghub")
    agpc = _node("n2", "agpc")
    agstudio = _node("n3", "agstudio")
    svc = _service("s1", "node_agent")
    placements = [
        _placement("p1", service_id="s1", node_id="n1", deployment_profile="daemon"),
        _placement("p2", service_id="s1", node_id="n2", deployment_profile="daemon"),
        _placement("p3", service_id="s1", node_id="n3", deployment_profile="daemon"),
    ]
    snapshot = _snapshot(nodes=[aghub, agpc, agstudio], services=[svc], placements=placements)

    diffs = [
        _service_observation_diff(svc, aghub),
        _service_observation_diff(svc, agpc),
        _service_observation_diff(svc, agstudio),
    ]

    scoped = select_scoped_diffs(diffs, PlanScope(kind="host", host_slug="aghub"), snapshot)

    assert len(scoped) == 1
    expected = scoped[0].desired["expected"]
    assert expected["node_slug"] == "aghub"

    plan = _build(snapshot, diffs, scope=PlanScope(kind="host", host_slug="aghub"))
    [action] = plan.actions
    assert action.reconciler_id == "observe_node"
    assert {t.slug for t in action.targets} == {"aghub"}


def test_select_scoped_diffs_node_local_and_service_observation_dedupe_to_one_target():
    aghub = _node("n1", "aghub")
    svc = _service("s1", "node_agent")
    placement = _placement("p1", service_id="s1", node_id="n1", deployment_profile="daemon")
    snapshot = _snapshot(nodes=[aghub], services=[svc], placements=[placement])

    diffs = [
        _node_diff(aghub, "missing_actual_data"),
        _service_observation_diff(svc, aghub),
    ]

    plan = _build(snapshot, diffs, scope=PlanScope(kind="host", host_slug="aghub"))
    [action] = plan.actions
    assert action.reconciler_id == "observe_node"
    assert {t.slug for t in action.targets} == {"aghub"}


def test_select_scoped_diffs_cluster_scope_retains_multi_host_observation():
    aghub = _node("n1", "aghub")
    agpc = _node("n2", "agpc")
    svc = _service("s1", "node_agent")
    placements = [
        _placement("p1", service_id="s1", node_id="n1", deployment_profile="daemon"),
        _placement("p2", service_id="s1", node_id="n2", deployment_profile="daemon"),
    ]
    snapshot = _snapshot(nodes=[aghub, agpc], services=[svc], placements=placements)
    diffs = [_service_observation_diff(svc, aghub), _service_observation_diff(svc, agpc)]

    scoped = select_scoped_diffs(diffs, CLUSTER, snapshot)
    assert len(scoped) == 2


def test_actual_without_desired_node_is_inert_and_never_plans_a_destructive_action():
    """Tier A: observations outside desired state are not deletion candidates.

    The real snapshot evaluator only evaluates declared DesiredNodes. Feeding an
    actual-only Device through it must produce no drift/action now or on a
    repeat; nctl has no reconciler permitted to delete, unlink, retire, or
    replace that observation merely because desired state is absent.
    """
    snapshot = _snapshot(devices=[ActualDevice(id="actual-only-1", name="unmanaged-device")])

    first_diffs = [diff for result in evaluate_all_nodes(snapshot).values() for diff in result.diffs]
    first = _build(snapshot, first_diffs)
    repeat_diffs = [diff for result in evaluate_all_nodes(snapshot).values() for diff in result.diffs]
    repeated = _build(snapshot, repeat_diffs)

    assert first_diffs == repeat_diffs == []
    assert first.actions == repeated.actions == []
    assert first.manual_review == repeated.manual_review == []
    assert first.unsupported == repeated.unsupported == []


def test_compute_create_defers_same_node_observation_until_after_actuation(monkeypatch):
    """Tier A: an absent guest never gets an SSH observation before its create action."""
    fixture = _node("n1", "agfixture")
    snapshot = _snapshot(nodes=[fixture])
    create = planner_module.ReconcileAction(
        id="create_compute_instance:agfixture", reconciler_id="create_compute_instance", action_kind="compute_create",
        targets=[Target(kind="compute_instance", slug="agfixture", id="instance")], claimed_diff_codes=["compute_instance_missing"],
        reason="test", mutates=True, requires_observation=True,
    )
    monkeypatch.setattr(planner_module, "plan_create_compute_instance", lambda *_args, **_kwargs: create)
    diffs = [
        DiffRecord(target=Target(kind="compute_instance", slug="agfixture", id="instance"), code="compute_instance_missing", severity=Severity.ERROR, message="missing"),
        _node_diff(fixture, "missing_actual_node"),
        _node_diff(fixture, "no_realized_device"),
    ]

    plan = _build(snapshot, diffs)

    assert [action.reconciler_id for action in plan.actions] == ["create_compute_instance"]


def test_compute_link_defers_same_node_observation_until_manual_initial_access(monkeypatch):
    """A newly linked running guest has no SSH path until manual console setup."""
    fixture = _node("n1", "agfixture")
    snapshot = _snapshot(nodes=[fixture])
    link = planner_module.ReconcileAction(
        id="link_compute_realization:agfixture", reconciler_id="link_compute_realization", action_kind="ledger_patch",
        targets=[Target(kind="compute_instance", slug="agfixture", id="instance")], claimed_diff_codes=["compute_instance_not_linked"],
        reason="test", mutates=True, requires_observation=False,
    )
    monkeypatch.setattr(planner_module, "plan_link_compute_realization", lambda *_args, **_kwargs: link)
    diffs = [
        DiffRecord(target=Target(kind="compute_instance", slug="agfixture", id="instance"), code="compute_instance_not_linked", severity=Severity.WARNING, message="unlinked"),
        _node_diff(fixture, "no_realized_device"),
    ]

    plan = _build(snapshot, diffs)

    assert [action.reconciler_id for action in plan.actions] == ["link_compute_realization"]


# --- link_actual_node / reconcile_ipam -------------------------------------


def test_link_actual_node_builds_ledger_patch_action_with_candidate():
    node = _node("n1", "agweb")
    device = ActualDevice(id="dev-1", name="agweb")
    snapshot = _snapshot(nodes=[node], devices=[device])
    diffs = [_node_diff(node, "actual_node_not_linked")]

    plan = _build(snapshot, diffs)

    [action] = plan.actions
    assert action.reconciler_id == "link_actual_node"
    assert action.action_kind == "ledger_patch"
    assert action.mutates is True
    assert action.requires_observation is False
    assert action.parameters["candidate"]["id"] == "dev-1"
    assert plan.manual_review == []
    assert plan.unsupported == []


def test_reconcile_ipam_action_depends_on_link_actual_node_for_same_node():
    node = _node("n1", "agweb")
    device = ActualDevice(id="dev-1", name="agweb")
    snapshot = _snapshot(nodes=[node], devices=[device])
    diffs = [
        _node_diff(node, "actual_node_not_linked"),
        _node_diff(node, "missing_actual_ip_address"),
    ]

    plan = _build(snapshot, diffs)

    by_reconciler = {action.reconciler_id: action for action in plan.actions}
    assert set(by_reconciler) == {"link_actual_node", "reconcile_ipam"}
    assert by_reconciler["reconcile_ipam"].dependencies == [by_reconciler["link_actual_node"].id]


def _ipam_endpoint_diff(node: DesiredNode, *, endpoint_id: str, code: str, ip_policy: str = "static") -> DiffRecord:
    return DiffRecord(
        target=Target(kind="node", slug=node.slug, name=node.name, id=node.id),
        code=code,
        severity=Severity.WARNING,
        message=f"{node.slug}: {code}",
        desired={
            "expected": {
                "endpoint_id": endpoint_id,
                "endpoint_name": f"endpoint-{endpoint_id}",
                "ip_policy": ip_policy,
                "ip_address": "192.0.2.10",
            }
        },
    )


def test_reconcile_ipam_action_pins_exact_eligible_endpoint_ids():
    node = _node("n1", "agweb")
    snapshot = _snapshot(nodes=[node])
    diffs = [
        _ipam_endpoint_diff(node, endpoint_id="e1", code="missing_actual_ip_address"),
        _ipam_endpoint_diff(node, endpoint_id="e2", code="actual_ip_address_not_linked"),
    ]

    plan = _build(snapshot, diffs)

    [action] = plan.actions
    assert action.reconciler_id == "reconcile_ipam"
    assert action.evidence["eligible_endpoint_ids"] == ["e1", "e2"]
    assert action.parameters["eligible_endpoint_ids"] == ["e1", "e2"]
    endpoints_by_id = {entry["endpoint_id"]: entry for entry in action.evidence["eligible_endpoints"]}
    assert endpoints_by_id["e1"]["gap_code"] == "missing_actual_ip_address"
    assert endpoints_by_id["e2"]["gap_code"] == "actual_ip_address_not_linked"


def test_reconcile_ipam_never_pins_a_manual_review_only_endpoint():
    node = _node("n1", "agweb")
    snapshot = _snapshot(nodes=[node])
    diffs = [
        _ipam_endpoint_diff(node, endpoint_id="e1", code="missing_actual_ip_address"),
        _ipam_endpoint_diff(node, endpoint_id="e2", code="ipam_reconcile_observation_missing"),
    ]

    plan = _build(snapshot, diffs)

    by_reconciler = {action.reconciler_id: action for action in plan.actions}
    assert by_reconciler["reconcile_ipam"].evidence["eligible_endpoint_ids"] == ["e1"]
    assert len(plan.manual_review) == 1
    assert plan.manual_review[0].code == "ipam_reconcile_observation_missing"


def test_desired_mac_mismatch_is_manual_review_never_an_automatic_action():
    """VM p3 Step 6: a `desired_mac_mismatch` diff never becomes an automatic
    reconcile action -- it is a conflict a human must resolve."""
    node = _node("n1", "agweb")
    snapshot = _snapshot(nodes=[node])
    diffs = [_node_diff(node, "desired_mac_mismatch")]

    plan = _build(snapshot, diffs)

    assert plan.actions == []
    [record] = plan.manual_review
    assert record.code == "desired_mac_mismatch"
    assert classify("desired_mac_mismatch", target_kind="node").classification == Classification.MANUAL_REVIEW


def test_link_actual_node_falls_back_to_manual_review_without_a_candidate():
    node = _node("n1", "agweb")
    snapshot = _snapshot(nodes=[node])  # no device candidates at all
    diffs = [_node_diff(node, "actual_node_not_linked")]

    plan = _build(snapshot, diffs)

    assert plan.actions == []
    [record] = plan.manual_review
    assert record.code == "actual_node_not_linked"


def test_link_actual_node_never_plans_a_virtual_machine_candidate():
    node = _node("n1", "agfixture", accepted_actual_types=["virtual_machine"])
    vm = ActualVirtualMachine(id="vm-109", name="agfixture")
    snapshot = _snapshot(nodes=[node], virtual_machines=[vm])
    diffs = [_node_diff(node, "actual_node_not_linked")]

    plan = _build(snapshot, diffs)

    assert plan.actions == []
    [record] = plan.manual_review
    assert record.code == "actual_node_not_linked"
    assert record.evidence["candidate"] == {"id": "vm-109", "name": "agfixture", "object_type": "virtualization.virtualmachine"}
    assert "DesiredComputeInstance.realized_vm" in record.reason


# --- service_profile / dnsmasq_config --------------------------------------


def test_service_profile_playbook_action():
    node = _node("n1", "agweb")
    svc = _service("s1", "grafana")
    placement = _placement("p1", service_id="s1", node_id="n1", deployment_profile="grafana")
    snapshot = _snapshot(nodes=[node], services=[svc], placements=[placement])
    diffs = [_service_diff(svc, "service_not_running")]
    reconciliation = {
        "grafana": ProfileReconciliation(
            action=ProfileAction(kind="playbook", playbook="playbooks/monitoring/setup_grafana.yml")
        )
    }

    plan = _build(snapshot, diffs, profile_reconciliation=reconciliation)

    [action] = plan.actions
    assert action.reconciler_id == "service_profile"
    assert action.action_kind == "playbook"
    assert action.parameters["playbook"] == "playbooks/monitoring/setup_grafana.yml"
    assert action.parameters["host_slugs"] == ["agweb"]
    assert action.requires_observation is True


def test_node_agent_profile_plans_only_the_scoped_placement_host():
    agstudio = _node("n1", "agstudio")
    agpc = _node("n2", "agpc")
    svc = _service("s1", "node-agent")
    placements = [
        _placement("p1", service_id="s1", node_id="n1", deployment_profile="node_agent"),
        _placement("p2", service_id="s1", node_id="n2", deployment_profile="node_agent"),
    ]
    snapshot = _snapshot(nodes=[agstudio, agpc], services=[svc], placements=placements)
    reconciliation = {
        "node_agent": ProfileReconciliation(
            action=ProfileAction(kind="playbook", playbook="playbooks/agent/setup_opencode.yml")
        )
    }

    plan = _build(
        snapshot,
        [_service_diff(svc, "service_not_running")],
        PlanScope(kind="host", host_slug="agpc"),
        reconciliation,
    )

    [action] = plan.actions
    assert action.reconciler_id == "service_profile"
    assert action.parameters["playbook"] == "playbooks/agent/setup_opencode.yml"
    assert action.parameters["host_slugs"] == ["agpc"]


def test_service_profile_action_excludes_manual_placement_hosts():
    agstudio = _node("n1", "agstudio")
    agautolab1 = _node("n2", "agautolab1")
    svc = _service("s1", "agautolab")
    placements = [
        _placement(
            "p1", service_id="s1", node_id="n1", deployment_profile="autolab_node"
        ).model_copy(update={"management_mode": "manual"}),
        _placement("p2", service_id="s1", node_id="n2", deployment_profile="autolab_node"),
    ]
    snapshot = _snapshot(nodes=[agstudio, agautolab1], services=[svc], placements=placements)
    reconciliation = {
        "autolab_node": ProfileReconciliation(
            action=ProfileAction(
                kind="playbook", playbook="playbooks/agent/setup_autolab_node.yml"
            )
        )
    }

    plan = _build(
        snapshot,
        [_service_diff(svc, "service_missing")],
        profile_reconciliation=reconciliation,
    )

    [action] = plan.actions
    assert action.parameters["host_slugs"] == ["agautolab1"]


def test_service_profile_dnsmasq_config_action():
    node = _node("n1", "agdnsmasq")
    svc = _service("s1", "dnsmasq")
    placement = _placement("p1", service_id="s1", node_id="n1", deployment_profile="dnsmasq")
    snapshot = _snapshot(nodes=[node], services=[svc], placements=[placement])
    diffs = [_service_diff(svc, "service_missing")]
    reconciliation = {"dnsmasq": ProfileReconciliation(action=ProfileAction(kind="dnsmasq_config"))}

    plan = _build(snapshot, diffs, profile_reconciliation=reconciliation)

    [action] = plan.actions
    assert action.reconciler_id == "dnsmasq_config"
    assert action.action_kind == "dnsmasq_config"
    # fix_sshkey3 Step 5: a dnsmasq deploy now always requires a
    # post-actuation observation, so the next round's drift compares
    # against the just-deployed digest.
    assert action.requires_observation is True


def test_service_profile_unsupported_when_profile_has_no_metadata():
    node = _node("n1", "agweb")
    svc = _service("s1", "mystery")
    placement = _placement("p1", service_id="s1", node_id="n1", deployment_profile="mystery")
    snapshot = _snapshot(nodes=[node], services=[svc], placements=[placement])
    diffs = [_service_diff(svc, "service_not_running")]

    plan = _build(snapshot, diffs, profile_reconciliation={})

    assert plan.actions == []
    [record] = plan.unsupported
    assert record.code == "service_not_running"


def test_service_profile_unsupported_when_observe_only():
    node = _node("n1", "aghaos")
    svc = _service("s1", "home_assistant")
    placement = _placement("p1", service_id="s1", node_id="n1", deployment_profile="home_assistant")
    snapshot = _snapshot(nodes=[node], services=[svc], placements=[placement])
    diffs = [_service_diff(svc, "service_missing")]
    reconciliation = {"home_assistant": ProfileReconciliation(observe_only=True)}

    plan = _build(snapshot, diffs, profile_reconciliation=reconciliation)

    assert plan.actions == []
    assert plan.unsupported[0].reason.startswith("deployment profile 'home_assistant' is observe_only")


def test_service_profile_manual_review_when_placements_disagree_on_profile():
    node_a = _node("n1", "agweb")
    node_b = _node("n2", "agweb2")
    svc = _service("s1", "confused")
    placements = [
        _placement("p1", service_id="s1", node_id="n1", deployment_profile="profile_a"),
        _placement("p2", service_id="s1", node_id="n2", deployment_profile="profile_b"),
    ]
    snapshot = _snapshot(nodes=[node_a, node_b], services=[svc], placements=placements)
    diffs = [_service_diff(svc, "service_not_running")]

    plan = _build(snapshot, diffs)

    assert plan.actions == []
    [record] = plan.manual_review
    assert "different deployment profiles" in record.reason


def test_profile_dependency_orders_actions_on_overlapping_hosts():
    node = _node("n1", "agmon")
    prometheus_svc = _service("s1", "prometheus")
    exporter_svc = _service("s2", "node_exporter")
    placements = [
        _placement("p1", service_id="s1", node_id="n1", deployment_profile="prometheus"),
        _placement("p2", service_id="s2", node_id="n1", deployment_profile="prometheus_node_exporter"),
    ]
    snapshot = _snapshot(nodes=[node], services=[prometheus_svc, exporter_svc], placements=placements)
    diffs = [
        _service_diff(prometheus_svc, "service_not_running"),
        _service_diff(exporter_svc, "service_not_running"),
    ]
    reconciliation = {
        "prometheus": ProfileReconciliation(
            action=ProfileAction(kind="playbook", playbook="playbooks/monitoring/setup_prometheus.yml")
        ),
        "prometheus_node_exporter": ProfileReconciliation(
            action=ProfileAction(kind="playbook", playbook="playbooks/monitoring/setup_node_exporter.yml"),
            dependencies=["prometheus"],
        ),
    }

    plan = _build(snapshot, diffs, profile_reconciliation=reconciliation)

    by_profile = {action.parameters["deployment_profile"]: action for action in plan.actions}
    exporter_action = by_profile["prometheus_node_exporter"]
    prometheus_action = by_profile["prometheus"]
    assert exporter_action.dependencies == [prometheus_action.id]
    order = [a.id for a in plan.actions]
    assert order.index(prometheus_action.id) < order.index(exporter_action.id)


# --- observe_node aggregation, fingerprint, and fail-closed classification -


def test_observe_node_aggregates_targets_and_codes():
    web = _node("n1", "agweb")
    db = _node("n2", "agdb")
    snapshot = _snapshot(nodes=[web, db])
    diffs = [
        _node_diff(web, "missing_actual_data"),
        _node_diff(db, "ingest_lag", Severity.INFO),
    ]

    plan = _build(snapshot, diffs)

    [action] = plan.actions
    assert action.id == "observe_node"
    assert action.reconciler_id == "observe_node"
    assert {t.slug for t in action.targets} == {"agweb", "agdb"}
    assert set(action.claimed_diff_codes) == {"missing_actual_data", "ingest_lag"}


def test_observe_node_resolves_service_target_to_owning_node():
    node = _node("n1", "agdnsmasq")
    svc = _service("s1", "dnsmasq")
    snapshot = _snapshot(nodes=[node], services=[svc], placements=[_placement("p1", service_id="s1", node_id="n1", deployment_profile="daemon")])
    diffs = [
        _service_observation_diff(svc, node),
        _node_diff(node, "missing_actual_data"),
    ]

    plan = _build(snapshot, diffs)

    [action] = plan.actions
    assert action.reconciler_id == "observe_node"
    assert [t.kind for t in action.targets] == ["node"]
    assert {t.slug for t in action.targets} == {"agdnsmasq"}
    assert set(action.claimed_diff_codes) == {"service_observation_missing", "missing_actual_data"}


def test_observe_node_resolves_service_target_alongside_unrelated_node():
    dnsmasq_node = _node("n1", "agdnsmasq")
    web_node = _node("n2", "agweb")
    svc = _service("s1", "dnsmasq")
    snapshot = _snapshot(
        nodes=[dnsmasq_node, web_node],
        services=[svc],
        placements=[_placement("p1", service_id="s1", node_id="n1", deployment_profile="daemon")],
    )
    diffs = [
        _service_observation_diff(svc, dnsmasq_node),
        _node_diff(web_node, "missing_actual_data"),
    ]

    plan = _build(snapshot, diffs)

    [action] = plan.actions
    assert action.reconciler_id == "observe_node"
    assert {t.kind for t in action.targets} == {"node"}
    assert {t.slug for t in action.targets} == {"agdnsmasq", "agweb"}


def test_observe_node_raises_when_service_diff_has_no_node_slug():
    svc = _service("s1", "dnsmasq")
    snapshot = _snapshot(services=[svc])
    diffs = [
        DiffRecord(
            target=Target(kind="service", slug=svc.slug, name=svc.name, id=svc.id),
            code="service_observation_missing",
            severity=Severity.ERROR,
            message="dnsmasq: service_observation_missing",
        )
    ]

    with pytest.raises(ValueError, match="node_slug"):
        _build(snapshot, diffs)


def test_fingerprint_ignores_non_error_diffs():
    web = _node("n1", "agweb")
    snapshot = _snapshot(nodes=[web])
    error_only = [_node_diff(web, "missing_actual_data")]
    with_info = error_only + [_node_diff(web, "ingest_lag", Severity.INFO)]

    assert compute_drift_fingerprint(error_only) == compute_drift_fingerprint(with_info)

    plan_error_only = _build(snapshot, error_only)
    plan_with_info = _build(snapshot, with_info)
    assert plan_error_only.drift_fingerprint == plan_with_info.drift_fingerprint
    # But the info diff still shows up as an extra observe_node target/code.
    assert len(plan_with_info.actions[0].claimed_diff_codes) > len(plan_error_only.actions[0].claimed_diff_codes)


def test_build_plan_raises_for_unclassified_error_diff():
    web = _node("n1", "agweb")
    snapshot = _snapshot(nodes=[web])
    diffs = [_node_diff(web, "brand_new_error_code_nobody_reviewed")]

    with pytest.raises(UnclassifiedDiffCodeError):
        _build(snapshot, diffs)


def test_build_plan_ignores_unclassified_non_error_diagnostic():
    web = _node("n1", "agweb")
    snapshot = _snapshot(nodes=[web])
    diffs = [_node_diff(web, "some_new_diagnostic_nobody_reviewed", Severity.INFO)]

    plan = _build(snapshot, diffs)

    assert plan.actions == []
    assert plan.manual_review == []
    assert plan.unsupported == []


# --- no_guest_vm Steps 1-2: hypervisor-routed evidence refresh and retired
# ---                        observe suppression ------------------------------

GENERATED_AT = "2026-07-17T00:00:00+00:00"


def _compute_snapshot(
    *,
    vm_present: bool,
    platform_observed_at: str = GENERATED_AT,
    guest_lifecycle: str = "retired",
    desired_presence: str = "absent",
    link: bool = False,
) -> SourceSnapshot:
    """One Proxmox platform: control node `aghub`, guest `agdoomed` (vmid 110)."""
    control = _node("control", "aghub", realized_device_id="device")
    guest = DesiredNode(
        id="guest-node", slug="agdoomed", name="agdoomed", lifecycle=guest_lifecycle,
        node_type="service_host", accepted_actual_types=["device"],
    )
    platform = DesiredComputePlatform(
        id="platform", name="aghub-pve", slug="aghub-pve", provider_type="proxmox", lifecycle="active",
        control_node_id="control", config_schema_version="v1",
        config={"cluster_name": "cluster"}, realized_cluster_id="cluster",
    )
    instance = DesiredComputeInstance(
        id="instance", desired_node_id="guest-node", platform_id="platform", instance_kind="container",
        desired_presence=desired_presence, desired_power_state="running",
        vcpus=1, memory_mb=512, root_disk_gb=8, config_schema_version="v1",
        config={"vmid": 110, "template": "local:vztmpl/ubuntu.tar.zst", "storage": "local-lvm", "bridge": "vmbr0"},
        realized_vm_id="vm" if link else None,
    )
    cluster = ActualCluster.model_validate({
        "id": "cluster", "name": "cluster",
        "proxmox": {"observer_device_id": "device", "observed_at": platform_observed_at,
                    "observation_state": "complete", "observed_node_names": ["aghub"]},
    })
    vms = []
    if vm_present:
        vms.append(ActualVirtualMachine.model_validate({
            "id": "vm", "name": "agdoomed", "cluster_id": "cluster", "vcpus": 1, "memory": 512, "disk": 8,
            "proxmox": {"guest_type": "lxc", "vmid": 110, "node": "aghub", "status": "running",
                        "presence": "present", "lxc_rootfs": {"storage": "local-lvm", "size_gb": 8}},
        }))
    return SourceSnapshot(
        desired=DesiredSnapshot(
            nodes=[control, guest], compute_platforms=[platform], compute_instances=[instance]
        ),
        actual=ActualSnapshot(clusters=[cluster], virtual_machines=vms),
        fetched_at=datetime.now(timezone.utc),
    )


def _instance_diff(code: str, severity: Severity = Severity.ERROR) -> DiffRecord:
    return DiffRecord(
        target=Target(kind="compute_instance", slug="agdoomed", name="agdoomed", id="instance"),
        code=code, severity=severity, message=f"agdoomed: {code}",
    )


def _guest_node_diff(code: str, severity: Severity = Severity.ERROR) -> DiffRecord:
    return DiffRecord(
        target=Target(kind="node", slug="agdoomed", name="agdoomed", id="guest-node"),
        code=code, severity=severity, message=f"agdoomed: {code}",
    )


def test_orphaned_guest_routes_evidence_refresh_to_control_node():
    """The F3 shape: guest exists on the hypervisor but not in Actual State,
    desired is retired/absent. The plan must contain an observe_node on the
    control node, none on the guest, and gate SSH on the control node only."""
    snapshot = _compute_snapshot(vm_present=False)
    diffs = [_instance_diff("compute_instance_missing"), _guest_node_diff("missing_actual_node")]

    plan = _build(snapshot, diffs, scope=PlanScope(kind="host", host_slug="agdoomed"))

    [action] = plan.actions
    assert action.id == "observe_node:compute-evidence"
    assert action.reconciler_id == "observe_node"
    assert [t.slug for t in action.targets] == ["aghub"]
    assert action.parameters["host_slugs"] == ["aghub"]
    assert "compute_instance_missing" in action.claimed_diff_codes
    assert ssh_required_host_slugs(plan) == {"aghub"}
    # The create fallback for the retired guest stays visible as manual review.
    assert "compute_instance_missing" in {r.code for r in plan.manual_review}


def test_stale_platform_observation_also_routes_refresh_to_control_node():
    """Acceptance (a): with a stale hypervisor snapshot, a retired guest's plan
    contains an observe_node on the control node and none on the guest."""
    snapshot = _compute_snapshot(vm_present=False, platform_observed_at="2026-07-01T00:00:00+00:00")
    diffs = [
        _instance_diff("compute_platform_observation_stale"),
        _guest_node_diff("missing_actual_node"),
    ]

    plan = _build(snapshot, diffs, scope=PlanScope(kind="host", host_slug="agdoomed"))

    observe_actions = [a for a in plan.actions if a.reconciler_id == "observe_node"]
    assert len(observe_actions) == 1
    assert [t.slug for t in observe_actions[0].targets] == ["aghub"]
    assert all("agdoomed" not in {t.slug for t in a.targets} for a in plan.actions)


def test_present_guest_plans_exactly_one_destroy_gated_on_control_node():
    """Acceptance (b): with the guest present in the hypervisor's guest list,
    the plan contains exactly one destroy_compute_instance and gates SSH on
    the control node only."""
    snapshot = _compute_snapshot(vm_present=True, link=True)
    diffs = [_instance_diff("compute_instance_destroy_required", Severity.WARNING)]

    plan = _build(snapshot, diffs, scope=PlanScope(kind="host", host_slug="agdoomed"))

    destroys = [a for a in plan.actions if a.reconciler_id == "destroy_compute_instance"]
    assert len(destroys) == 1
    assert destroys[0].parameters["host_slugs"] == ["aghub"]
    assert [a.reconciler_id for a in plan.actions] == ["destroy_compute_instance"]
    assert ssh_required_host_slugs(plan) == {"aghub"}


def test_unlinked_present_guest_plans_link_before_destroy():
    """no_guest_vm G2: a retired guest whose VM matched only by vmid gets its
    ledger link planned in the same round as -- and ahead of -- the destroy,
    so the destroy operates on a linked row and prune can collect it."""
    snapshot = _compute_snapshot(vm_present=True, link=False)
    diffs = [
        _instance_diff("compute_instance_not_linked", Severity.WARNING),
        _instance_diff("compute_instance_destroy_required", Severity.WARNING),
    ]

    plan = _build(snapshot, diffs, scope=PlanScope(kind="host", host_slug="agdoomed"))

    assert [a.reconciler_id for a in plan.actions] == ["link_compute_realization", "destroy_compute_instance"]
    link, destroy = plan.actions
    assert link.parameters["virtual_machine_id"] == "vm"
    assert link.parameters["match_basis"] == "vmid"
    assert destroy.parameters["host_slugs"] == ["aghub"]


def test_removal_complete_unlinked_tombstone_plans_only_the_link():
    """no_guest_vm G2 (tombstone cleanup shape): the VM is already absent on
    the hypervisor; the plan records the link so prune can collect the row."""
    snapshot = _compute_snapshot(vm_present=True, link=False)
    snapshot.actual.virtual_machines[0] = snapshot.actual.virtual_machines[0].model_copy(
        update={"proxmox": snapshot.actual.virtual_machines[0].proxmox.model_copy(update={"presence": "absent", "status": "stopped"})}
    )
    diffs = [_instance_diff("compute_instance_not_linked", Severity.WARNING)]

    plan = _build(snapshot, diffs, scope=PlanScope(kind="host", host_slug="agdoomed"))

    assert [a.reconciler_id for a in plan.actions] == ["link_compute_realization"]
    assert plan.actions[0].parameters["virtual_machine_id"] == "vm"


def test_realized_instance_never_triggers_a_control_node_refresh():
    snapshot = _compute_snapshot(vm_present=True, link=True)
    diffs = [_instance_diff("compute_instance_destroy_required", Severity.WARNING)]

    plan = _build(snapshot, diffs, scope=PlanScope(kind="host", host_slug="agdoomed"))

    assert all(a.id != "observe_node:compute-evidence" for a in plan.actions)


def test_active_guest_with_failed_create_preflight_gets_control_node_refresh():
    """The narrowing rule: only when no create action was planned. Creation
    preflight fails here (no template/storage/bridge evidence), so the create
    falls back to manual_review and the control-node refresh is planned."""
    snapshot = _compute_snapshot(vm_present=False, guest_lifecycle="active", desired_presence="present")
    diffs = [_instance_diff("compute_instance_missing")]

    plan = _build(snapshot, diffs, scope=PlanScope(kind="host", host_slug="agdoomed"))

    assert [a.id for a in plan.actions] == ["observe_node:compute-evidence"]
    assert "compute_instance_missing" in {r.code for r in plan.manual_review}


def test_active_unrealized_guest_is_not_observe_gated_before_creation():
    """no_guest_vm G3: an active guest with no VM in its realization and no
    realized Device has never run sshd -- a guest-targeted observe_node can
    only fail unenrolled. Its refresh routes to the control node instead."""
    snapshot = _compute_snapshot(vm_present=False, guest_lifecycle="active", desired_presence="present",
                                 platform_observed_at="2026-07-01T00:00:00+00:00")
    diffs = [
        _instance_diff("compute_platform_observation_stale"),
        _guest_node_diff("missing_actual_node"),
    ]

    plan = _build(snapshot, diffs, scope=PlanScope(kind="host", host_slug="agdoomed"))

    observe_actions = [a for a in plan.actions if a.reconciler_id == "observe_node"]
    assert len(observe_actions) == 1
    assert observe_actions[0].id == "observe_node:compute-evidence"
    assert [t.slug for t in observe_actions[0].targets] == ["aghub"]
    assert all("agdoomed" not in {t.slug for t in a.targets} for a in plan.actions)


def test_unrealized_guest_with_realized_device_keeps_its_own_observe_action():
    """The G3 narrowing: a guest that has a realized Device exists and is
    enrolled; its stale facts are refreshed by observing the guest itself."""
    snapshot = _compute_snapshot(vm_present=False, guest_lifecycle="active", desired_presence="present",
                                 platform_observed_at="2026-07-01T00:00:00+00:00")
    guest = next(node for node in snapshot.desired.nodes if node.slug == "agdoomed")
    snapshot.desired.nodes[snapshot.desired.nodes.index(guest)] = guest.model_copy(
        update={"realized_device_id": "guest-device"}
    )
    diffs = [_guest_node_diff("stale_actual_data")]

    plan = _build(snapshot, diffs, scope=PlanScope(kind="host", host_slug="agdoomed"))

    observe_actions = [a for a in plan.actions if a.reconciler_id == "observe_node" and a.id != "observe_node:compute-evidence"]
    assert len(observe_actions) == 1
    assert {t.slug for t in observe_actions[0].targets} == {"agdoomed"}


def test_retired_node_never_gets_an_observe_action(monkeypatch):
    """no_guest_vm Step 2: a retired node's evidence gaps stay visible in
    drift but never produce an SSH-gated observe_node action."""
    retired = DesiredNode(
        id="n1", slug="aggone", name="aggone", lifecycle="retired", node_type="device",
        accepted_actual_types=["device"],
    )
    active = _node("n2", "aglive")
    snapshot = _snapshot(nodes=[retired, active])
    diffs = [
        _node_diff(retired, "missing_actual_node"),
        _node_diff(active, "missing_actual_data"),
    ]

    plan = _build(snapshot, diffs)

    [action] = plan.actions
    assert action.reconciler_id == "observe_node"
    assert {t.slug for t in action.targets} == {"aglive"}


def test_retired_only_observation_diffs_produce_no_observe_action():
    retired = DesiredNode(
        id="n1", slug="aggone", name="aggone", lifecycle="retired", node_type="device",
        accepted_actual_types=["device"],
    )
    snapshot = _snapshot(nodes=[retired])
    diffs = [_node_diff(retired, "missing_actual_node")]

    plan = _build(snapshot, diffs)

    assert plan.actions == []


def test_retired_platform_lifecycle_also_suppresses_guest_observation():
    """Effective lifecycle combines node and platform (compute.contract):
    an active guest under a retired platform is effectively retired."""
    snapshot = _compute_snapshot(vm_present=False, guest_lifecycle="active")
    snapshot.desired.compute_platforms[0] = snapshot.desired.compute_platforms[0].model_copy(
        update={"lifecycle": "retired"}
    )
    diffs = [_guest_node_diff("missing_actual_node")]

    plan = _build(snapshot, diffs, scope=PlanScope(kind="host", host_slug="agdoomed"))

    assert all("agdoomed" not in {t.slug for t in a.targets} for a in plan.actions)


# --- Phase 1 (better_usability p1): production-blocked host filtering ------


def test_service_action_excludes_a_production_blocked_host():
    healthy = _node("n1", "aghealthy")
    blocked = _node("n2", "agblocked")
    svc = _service("s1", "web")
    p1 = _placement("p1", service_id="s1", node_id="n1", deployment_profile="web")
    p2 = _placement("p2", service_id="s1", node_id="n2", deployment_profile="web")
    snapshot = _snapshot(nodes=[healthy, blocked], services=[svc], placements=[p1, p2])
    diffs = [
        _service_diff(svc, "service_not_running"),
        DiffRecord(
            target=Target(kind="node", slug="agblocked", name="agblocked", id="n2"),
            code="unknown_profile",
            severity=Severity.ERROR,
            message="agblocked: unknown_profile",
            desired={"placement": {"id": "p2", "instance_name": "web-n2", "config": {}}},
        ),
    ]
    reconciliation = {"web": ProfileReconciliation(action=ProfileAction(kind="playbook", playbook="playbooks/web.yml"))}

    plan = _build(snapshot, diffs, profile_reconciliation=reconciliation)

    [action] = plan.actions
    assert action.parameters["host_slugs"] == ["aghealthy"]
    manual_codes = {(r.target.slug, r.code) for r in plan.manual_review}
    assert ("agblocked", "unknown_profile") in manual_codes
    # The blocked node's manual-review record still carries the placement
    # evidence -- the reason is never silently erased by the filtering.
    blocked_record = next(r for r in plan.manual_review if r.target.slug == "agblocked")
    assert blocked_record.evidence["desired"]["placement"]["id"] == "p2"


def test_ambiguous_endpoint_blocks_only_its_host_in_cluster_and_host_scopes():
    healthy = _node("n1", "aghealthy")
    blocked = _node("n2", "agblocked")
    svc = _service("s1", "web")
    placements = [
        _placement("p1", service_id="s1", node_id="n1", deployment_profile="web"),
        _placement("p2", service_id="s1", node_id="n2", deployment_profile="web"),
    ]
    snapshot = _snapshot(nodes=[healthy, blocked], services=[svc], placements=placements)
    diffs = [
        _service_diff(svc, "service_not_running"),
        DiffRecord(
            target=Target(kind="node", slug="agblocked", name="agblocked", id="n2"),
            code="ambiguous_connection_endpoints", severity=Severity.ERROR,
            message="agblocked: multiple local endpoints have equal precedence",
        ),
    ]
    reconciliation = {
        "web": ProfileReconciliation(action=ProfileAction(kind="playbook", playbook="playbooks/web.yml"))
    }

    cluster_plan = _build(snapshot, diffs, profile_reconciliation=reconciliation)
    assert cluster_plan.actions[0].parameters["host_slugs"] == ["aghealthy"]
    assert [record.code for record in cluster_plan.manual_review] == ["ambiguous_connection_endpoints"]

    healthy_plan = _build(
        snapshot, diffs, scope=PlanScope(kind="host", host_slug="aghealthy"),
        profile_reconciliation=reconciliation,
    )
    assert healthy_plan.actions[0].parameters["host_slugs"] == ["aghealthy"]
    assert healthy_plan.manual_review == []

    blocked_plan = _build(
        snapshot, diffs, scope=PlanScope(kind="host", host_slug="agblocked"),
        profile_reconciliation=reconciliation,
    )
    assert blocked_plan.actions == []
    assert [record.code for record in blocked_plan.manual_review] == ["ambiguous_connection_endpoints"]


def test_intent_effect_summary_info_is_omitted_from_reconcile_plan():
    node = _node("n1", "agweb")
    diff = DiffRecord(
        target=Target(kind="node", slug="agweb", name="agweb", id="n1"),
        code="intent_effect_summary", severity=Severity.INFO,
        message="agweb: recorded intent, effective mechanism, and production application",
    )

    plan = _build(_snapshot(nodes=[node]), [diff])

    assert plan.actions == []
    assert plan.manual_review == []
    assert plan.unsupported == []


def test_deployment_profiles_unavailable_is_a_global_blocking_finding():
    # Phase 4 Decision 3: a global ERROR deployment_profiles_unavailable diff is
    # classified MANUAL_REVIEW like every other global code (classify()'s
    # target_kind == "global" branch). The executor (Decision 5) refuses to
    # execute *any* action while plan.has_global_blocking_findings() is true,
    # regardless of other healthy nodes' own automatable diffs.
    healthy = _node("n1", "aghealthy")
    diffs = [
        _global_diff("deployment_profiles_unavailable"),
        _node_diff(healthy, "actual_node_not_linked"),
    ]

    plan = _build(_snapshot(nodes=[healthy]), diffs)

    assert plan.has_global_blocking_findings() is True
    codes = {record.code for record in plan.manual_review}
    assert "deployment_profiles_unavailable" in codes


def test_service_action_omitted_when_every_host_is_blocked():
    blocked = _node("n1", "agblocked")
    svc = _service("s1", "web")
    p1 = _placement("p1", service_id="s1", node_id="n1", deployment_profile="web")
    snapshot = _snapshot(nodes=[blocked], services=[svc], placements=[p1])
    diffs = [
        _service_diff(svc, "service_not_running"),
        DiffRecord(
            target=Target(kind="node", slug="agblocked", name="agblocked", id="n1"),
            code="unresolved_connection_path",
            severity=Severity.ERROR,
            message="agblocked: unresolved_connection_path",
        ),
    ]
    reconciliation = {"web": ProfileReconciliation(action=ProfileAction(kind="playbook", playbook="playbooks/web.yml"))}

    plan = _build(snapshot, diffs, profile_reconciliation=reconciliation)

    assert plan.actions == []
    assert plan.unsupported == []
    codes = {r.code for r in plan.manual_review}
    assert "unresolved_connection_path" in codes


def test_unrelated_automatic_action_survives_alongside_a_blocked_node():
    healthy = _node("n1", "aghealthy", realized_device_id="dev-1")
    blocked = _node("n2", "agblocked")
    device = ActualDevice(id="dev-1", name="aghealthy.local")
    snapshot = _snapshot(nodes=[healthy, blocked], devices=[device])
    diffs = [
        _node_diff(healthy, "actual_node_not_linked"),
        DiffRecord(
            target=Target(kind="node", slug="agblocked", name="agblocked", id="n2"),
            code="invalid_platform_power",
            severity=Severity.ERROR,
            message="agblocked: invalid_platform_power",
        ),
    ]

    plan = _build(snapshot, diffs)

    assert plan.has_local_blocking_findings() is True
    assert plan.has_global_blocking_findings() is False
    reconciler_ids = {a.reconciler_id for a in plan.actions}
    assert "link_actual_node" in reconciler_ids


def test_host_scoped_reconcile_selects_only_that_host_blocker():
    healthy = _node("n1", "aghealthy")
    blocked = _node("n2", "agblocked")
    snapshot = _snapshot(nodes=[healthy, blocked])
    diffs = [
        DiffRecord(
            target=Target(kind="node", slug="agblocked", name="agblocked", id="n2"),
            code="unresolved_connection_path",
            severity=Severity.ERROR,
            message="agblocked: unresolved_connection_path",
        ),
    ]

    plan = _build(snapshot, diffs, scope=PlanScope(kind="host", host_slug="aghealthy"))
    assert plan.manual_review == []

    plan = _build(snapshot, diffs, scope=PlanScope(kind="host", host_slug="agblocked"))
    assert [r.code for r in plan.manual_review] == ["unresolved_connection_path"]


def test_every_production_blocking_node_code_reaches_planning_without_unclassified_error():
    from nctl_core.production.composer import PRODUCTION_BLOCKING_NODE_CODES

    node = _node("n1", "agx")
    snapshot = _snapshot(nodes=[node])
    for code in sorted(PRODUCTION_BLOCKING_NODE_CODES):
        severity = Severity.WARNING if code == "active_placement_not_applied" else Severity.ERROR
        diff = DiffRecord(
            target=Target(kind="node", slug="agx", name="agx", id="n1"),
            code=code, severity=severity, message=f"agx: {code}",
        )
        plan = _build(snapshot, [diff])  # must not raise UnclassifiedDiffCodeError
        classified = classify(code, target_kind="node")
        if classified.classification == Classification.MANUAL_REVIEW:
            assert [r.code for r in plan.manual_review] == [code]
            assert plan.actions == []
        else:
            assert plan.manual_review == []
            assert [action.reconciler_id for action in plan.actions] == ["observe_node"]
