"""Phase 4 Step 4.7: the mixed good/bad node matrix, run through the real pipeline
(compute_drift -> build_plan), not synthetic diffs. Six nodes in one snapshot/plan:

- aghealthy: converged, one active placement on a valid profile.
- agambiguous: two usable local endpoints, neither marked primary -> ambiguous_connection_endpoints.
- agstale: stale nodeutils observation -> stale_actual_data.
- agbadconfig: an active placement on an unknown deployment profile -> unknown_profile.
- agplanned: lifecycle-ineligible with an active placement -> out_of_scope,
  active_placement_not_applied (WARNING, stays converged per Decision 4).
- agcontainer: node_type-ineligible (container), no realized object -> intent_effect_summary
  reports production.state=out_of_scope (independent of whatever the separate identity-
  evaluation layer finds about the missing realized link).

Asserts the healthy node's own status/diffs/plan actions are unaffected by any neighbor
(cluster scope and host scope), every bad node gets its own precise diff, and every
intent_effect_summary INFO diff is excluded from both plan scopes.
"""

from __future__ import annotations

from datetime import datetime, timezone

from nctl_core.drift.context import DriftContext
from nctl_core.drift.engine import compute_drift
from nctl_core.drift.model import Status
from nctl_core.reconcile.planner import build_plan
from nctl_core.reconcile.model import PlanScope
from nctl_core.sources.actual import ActualDevice, ActualSnapshot
from nctl_core.sources.desired import DesiredEndpoint, DesiredNode, DesiredService, DesiredServicePlacement, DesiredSnapshot
from nctl_core.sources.snapshot import SourceSnapshot

FRESH = "2026-07-15T11:00:00+00:00"
GENERATED_AT = "2026-07-15T12:00:00+00:00"

PROFILES = {
    "web": {
        "group": "web_server",
        "config_schema_version": "1",
        "variables": {"enabled": {"ansible_variable": "web_enabled", "type": "boolean", "required": False}},
    }
}


def _primary_endpoint(node_id: str, slug: str) -> DesiredEndpoint:
    return DesiredEndpoint(
        id=f"endpoint-{node_id}", name="primary", endpoint_type="primary",
        node_id=node_id, node_slug=slug, dns_name=f"{slug}.example.test",
    )


def _device(device_id: str, name: str, *, collected_at: str = FRESH) -> ActualDevice:
    return ActualDevice(
        id=device_id, name=name,
        facts={"host_system": "Linux", "last_seen": collected_at},
    )


def make_snapshot() -> SourceSnapshot:
    healthy = DesiredNode(
        id="n1", slug="aghealthy", name="aghealthy", lifecycle="active", node_type="device",
        realized_device_id="dev-1",
    )
    ambiguous = DesiredNode(
        id="n2", slug="agambiguous", name="agambiguous", lifecycle="active", node_type="device",
        realized_device_id="dev-2",
    )
    stale = DesiredNode(
        id="n3", slug="agstale", name="agstale", lifecycle="active", node_type="device",
        realized_device_id="dev-3",
    )
    badconfig = DesiredNode(
        id="n4", slug="agbadconfig", name="agbadconfig", lifecycle="active", node_type="device",
        realized_device_id="dev-4",
    )
    planned = DesiredNode(
        id="n5", slug="agplanned", name="agplanned", lifecycle="planned", node_type="device",
        realized_device_id="dev-5",
    )
    container = DesiredNode(
        id="n6", slug="agcontainer", name="agcontainer", lifecycle="active", node_type="container",
        accepted_actual_types=["container"],
    )

    endpoints = [
        _primary_endpoint("n1", "aghealthy"),
        DesiredEndpoint(
            id="endpoint-n2-a", name="mgmt-a", endpoint_type="management",
            node_id="n2", node_slug="agambiguous", dns_name="agambiguous-a.example.test",
        ),
        DesiredEndpoint(
            id="endpoint-n2-b", name="mgmt-b", endpoint_type="management",
            node_id="n2", node_slug="agambiguous", dns_name="agambiguous-b.example.test",
        ),
        _primary_endpoint("n3", "agstale"),
        _primary_endpoint("n4", "agbadconfig"),
        _primary_endpoint("n5", "agplanned"),
    ]

    service = DesiredService(
        id="s1", slug="web", name="web", display_name="Web", service_type="service",
        lifecycle="active", catalog_namespace="default", catalog_metadata_name="web",
    )
    placements = [
        DesiredServicePlacement(
            id="p1", service_id="s1", node_id="n1", instance_name="primary",
            deployment_profile="web", config_schema_version="1", config={"enabled": True},
        ),
        DesiredServicePlacement(
            id="p4", service_id="s1", node_id="n4", instance_name="primary",
            deployment_profile="totally-unknown-profile", config_schema_version="1", config={},
        ),
        DesiredServicePlacement(
            id="p5", service_id="s1", node_id="n5", instance_name="primary",
            deployment_profile="web", config_schema_version="1", config={"enabled": True},
        ),
    ]

    devices = [
        _device("dev-1", "aghealthy.local"),
        _device("dev-2", "agambiguous.local"),
        _device("dev-3", "agstale.local", collected_at="2026-06-01T00:00:00+00:00"),  # stale
        _device("dev-4", "agbadconfig.local"),
        _device("dev-5", "agplanned.local"),
    ]

    return SourceSnapshot(
        desired=DesiredSnapshot(
            nodes=[healthy, ambiguous, stale, badconfig, planned, container],
            endpoints=endpoints,
            services=[service],
            placements=placements,
        ),
        actual=ActualSnapshot(devices=devices),
        fetched_at=datetime.now(timezone.utc),
    )


def test_mixed_six_node_matrix_isolates_every_finding_through_drift_and_reconcile():
    snapshot = make_snapshot()
    context = DriftContext(generated_at=GENERATED_AT, profiles=PROFILES)

    result = compute_drift(snapshot, context)
    by_slug = {t.target.slug: t for t in result.targets if t.target.kind == "node"}

    # -- drift: each node gets exactly its own finding, nothing bleeds across --
    assert by_slug["aghealthy"].status == Status.CONVERGED
    healthy_codes = {d.code for d in by_slug["aghealthy"].diffs}
    assert "intent_effect_summary" in healthy_codes
    assert healthy_codes & {
        "ambiguous_connection_endpoints", "stale_actual_data", "unknown_profile", "active_placement_not_applied",
    } == set()

    assert by_slug["agambiguous"].status == Status.DRIFTING
    assert "ambiguous_connection_endpoints" in {d.code for d in by_slug["agambiguous"].diffs}

    # Stale observation data is "we can't tell yet", not "known disagreement" -- unknown, not drifting.
    assert by_slug["agstale"].status == Status.UNKNOWN
    assert "stale_actual_data" in {d.code for d in by_slug["agstale"].diffs}

    assert by_slug["agbadconfig"].status == Status.DRIFTING
    assert "unknown_profile" in {d.code for d in by_slug["agbadconfig"].diffs}

    # WARNING-only stays converged (Decision 4); the finding is still visible.
    assert by_slug["agplanned"].status == Status.CONVERGED
    planned_diffs = {d.code: d for d in by_slug["agplanned"].diffs}
    assert planned_diffs["active_placement_not_applied"].severity.value == "warning"

    # Node-type-only ineligibility is reported through intent_effect_summary's
    # production.state, independent of whatever the identity-evaluation layer
    # separately finds about this node having no realized object at all.
    container_summary = next(d for d in by_slug["agcontainer"].diffs if d.code == "intent_effect_summary")
    assert container_summary.actual["production"]["state"] == "out_of_scope"

    all_diffs = [d for t in result.targets for d in t.diffs]

    # -- reconcile: cluster scope --
    cluster_plan = build_plan(
        snapshot=snapshot, diffs=all_diffs, scope=PlanScope(kind="cluster"),
        drift_generated_at=GENERATED_AT, profile_reconciliation={},
    )
    assert not cluster_plan.has_global_blocking_findings()
    node_manual_review = {
        r.target.slug: r.code for r in cluster_plan.manual_review if r.target.kind == "node"
    }
    # agstale (stale_actual_data) is OBSERVATION-classified, not MANUAL_REVIEW -- it becomes
    # an observe_node action instead, confirmed separately below.
    assert node_manual_review == {
        "agambiguous": "ambiguous_connection_endpoints",
        "agbadconfig": "unknown_profile",
        "agplanned": "active_placement_not_applied",
        "agcontainer": "no_realized_object",
    }
    assert "aghealthy" not in node_manual_review
    assert all(record.code != "intent_effect_summary" for record in cluster_plan.manual_review)
    assert any(action.reconciler_id == "observe_node" for action in cluster_plan.actions)

    # -- reconcile: host scope, isolated to the healthy node only --
    # aghealthy's own placement of "web" has no observed-running evidence in this fixture, so
    # its manual_review may legitimately include the service's own service_missing record --
    # what matters is that none of the *other* nodes' findings leak into this host's plan.
    host_plan = build_plan(
        snapshot=snapshot, diffs=all_diffs, scope=PlanScope(kind="host", host_slug="aghealthy"),
        drift_generated_at=GENERATED_AT, profile_reconciliation={},
    )
    host_manual_review_targets = {(r.target.kind, r.target.slug) for r in host_plan.manual_review}
    assert host_manual_review_targets <= {("service", "web")}
    assert host_plan.unsupported == []
