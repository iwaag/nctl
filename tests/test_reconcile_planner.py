from __future__ import annotations

from datetime import datetime, timezone

import pytest

from nctl_core.drift.model import DiffRecord, Severity, Target
from nctl_core.reconcile.classify import UnclassifiedDiffCodeError, classify
from nctl_core.reconcile.fingerprint import compute_drift_fingerprint
from nctl_core.reconcile.planner import HostScopeError, build_plan, select_scoped_diffs
from nctl_core.reconcile.model import Classification, PlanScope
from nctl_core.reconcile.profiles import ProfileAction, ProfileReconciliation
from nctl_core.sources.actual import ActualDevice, ActualSnapshot
from nctl_core.sources.desired import DesiredNode, DesiredService, DesiredServicePlacement, DesiredSnapshot
from nctl_core.sources.snapshot import SourceSnapshot


def _node(node_id: str, slug: str, *, realized_device_id: str | None = None) -> DesiredNode:
    return DesiredNode(
        id=node_id,
        slug=slug,
        name=slug,
        lifecycle="active",
        node_type="device",
        accepted_actual_types=["device"],
        realized_device_id=realized_device_id,
    )


def _service(service_id: str, slug: str) -> DesiredService:
    return DesiredService(
        id=service_id,
        slug=slug,
        name=slug,
        display_name=slug,
        service_type="daemon",
        lifecycle="active",
        catalog_namespace="ns",
        catalog_metadata_name=slug,
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


def _snapshot(*, nodes=(), devices=(), services=(), placements=()) -> SourceSnapshot:
    return SourceSnapshot(
        desired=DesiredSnapshot(nodes=list(nodes), services=list(services), placements=list(placements)),
        actual=ActualSnapshot(devices=list(devices)),
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


def test_link_actual_node_falls_back_to_manual_review_without_a_candidate():
    node = _node("n1", "agweb")
    snapshot = _snapshot(nodes=[node])  # no device candidates at all
    diffs = [_node_diff(node, "actual_node_not_linked")]

    plan = _build(snapshot, diffs)

    assert plan.actions == []
    [record] = plan.manual_review
    assert record.code == "actual_node_not_linked"


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
