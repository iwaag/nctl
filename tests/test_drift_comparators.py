from __future__ import annotations

from datetime import datetime, timezone

from nctl_core.drift import comparators
from nctl_core.drift.context import DriftContext
from nctl_core.sources.actual import ActualDevice, ActualInterface, ActualIPAddress, ActualSnapshot, ActualVirtualMachine
from nctl_core.sources.desired import (
    DesiredDependency,
    DesiredEndpoint,
    DesiredNode,
    DesiredNodeOperationalConfig,
    DesiredService,
    DesiredServicePlacement,
    DesiredSnapshot,
)
from nctl_core.sources.observed import ObservedFacts
from nctl_core.sources.snapshot import SourceSnapshot

CONTEXT = DriftContext(generated_at="2026-07-15T12:00:00+00:00")


def make_snapshot(
    *,
    nodes=(),
    endpoints=(),
    ip_ranges=(),
    services=(),
    dependencies=(),
    placements=(),
    operational_configs=(),
    devices=(),
    vms=(),
    interfaces=(),
    ip_addresses=(),
    observed=(),
) -> SourceSnapshot:
    return SourceSnapshot(
        desired=DesiredSnapshot(
            nodes=list(nodes),
            endpoints=list(endpoints),
            ip_ranges=list(ip_ranges),
            services=list(services),
            dependencies=list(dependencies),
            placements=list(placements),
            operational_configs=list(operational_configs),
        ),
        actual=ActualSnapshot(devices=list(devices), virtual_machines=list(vms), interfaces=list(interfaces), ip_addresses=list(ip_addresses)),
        observed=list(observed),
        fetched_at=datetime.now(timezone.utc),
    )


# --- node_existence -------------------------------------------------------


def test_node_existence_flags_dangling_realized_device():
    node = DesiredNode(id="n1", slug="agweb", name="agweb", lifecycle="active", node_type="device", realized_device_id="dev-gone")
    snapshot = make_snapshot(nodes=[node])

    diffs = list(comparators.node_existence(snapshot, CONTEXT))

    assert len(diffs) == 1
    assert diffs[0].code == "realized_device_missing"
    assert diffs[0].target.slug == "agweb"


def test_node_existence_flags_dangling_realized_vm():
    node = DesiredNode(id="n1", slug="agvm", name="agvm", lifecycle="active", node_type="virtual_machine", realized_vm_id="vm-gone")
    snapshot = make_snapshot(nodes=[node])

    diffs = list(comparators.node_existence(snapshot, CONTEXT))

    assert [d.code for d in diffs] == ["realized_vm_missing"]


def test_node_existence_ok_when_realized_device_exists():
    node = DesiredNode(id="n1", slug="agweb", name="agweb", lifecycle="active", node_type="device", realized_device_id="dev-1")
    device = ActualDevice(id="dev-1", name="agweb.local")
    snapshot = make_snapshot(nodes=[node], devices=[device])

    assert list(comparators.node_existence(snapshot, CONTEXT)) == []


def test_node_existence_flags_required_policy_with_no_realized_object():
    node = DesiredNode(id="n1", slug="agweb", name="agweb", lifecycle="active", node_type="device")
    op_config = DesiredNodeOperationalConfig(id="op1", node_id="n1", actual_state_policy="required", connection_path="local")
    snapshot = make_snapshot(nodes=[node], operational_configs=[op_config])

    diffs = list(comparators.node_existence(snapshot, CONTEXT))

    assert [d.code for d in diffs] == ["no_realized_object"]


def test_node_existence_allows_declared_policy_with_no_realized_object():
    node = DesiredNode(id="n1", slug="aghaos", name="aghaos", lifecycle="active", node_type="device")
    op_config = DesiredNodeOperationalConfig(
        id="op1", node_id="n1", actual_state_policy="declared", connection_path="local", declared_host_os="haos"
    )
    snapshot = make_snapshot(nodes=[node], operational_configs=[op_config])

    assert list(comparators.node_existence(snapshot, CONTEXT)) == []


# --- ingest_lag -------------------------------------------------------


def test_ingest_lag_flags_dump_newer_than_last_seen():
    device = ActualDevice(id="dev-1", name="agweb.local", facts={"last_seen": "2026-07-14T00:00:00+00:00"})
    observed = ObservedFacts(hostname="agweb.local", collected_at=datetime(2026, 7, 15, 0, 0, tzinfo=timezone.utc))
    snapshot = make_snapshot(devices=[device], observed=[observed])

    diffs = list(comparators.ingest_lag(snapshot, CONTEXT))

    assert [d.code for d in diffs] == ["ingest_lag"]
    assert diffs[0].target.kind == "device"
    assert diffs[0].severity.value == "info"


def test_ingest_lag_attributes_to_desired_node_when_linked():
    node = DesiredNode(id="n1", slug="agweb", name="agweb", lifecycle="active", node_type="device", realized_device_id="dev-1")
    device = ActualDevice(id="dev-1", name="agweb.local", facts={"last_seen": "2026-07-14T00:00:00+00:00"})
    observed = ObservedFacts(hostname="agweb.local", collected_at=datetime(2026, 7, 15, 0, 0, tzinfo=timezone.utc))
    snapshot = make_snapshot(nodes=[node], devices=[device], observed=[observed])

    diffs = list(comparators.ingest_lag(snapshot, CONTEXT))

    assert diffs[0].target.kind == "node"
    assert diffs[0].target.slug == "agweb"


def test_ingest_lag_silent_when_dump_is_not_newer():
    device = ActualDevice(id="dev-1", name="agweb.local", facts={"last_seen": "2026-07-15T00:00:00+00:00"})
    observed = ObservedFacts(hostname="agweb.local", collected_at=datetime(2026, 7, 14, 0, 0, tzinfo=timezone.utc))
    snapshot = make_snapshot(devices=[device], observed=[observed])

    assert list(comparators.ingest_lag(snapshot, CONTEXT)) == []


def test_ingest_lag_silent_when_no_matching_device():
    observed = ObservedFacts(hostname="unknown.local", collected_at=datetime(2026, 7, 15, 0, 0, tzinfo=timezone.utc))
    snapshot = make_snapshot(observed=[observed])

    assert list(comparators.ingest_lag(snapshot, CONTEXT)) == []


def test_ingest_lag_flags_never_ingested_device():
    device = ActualDevice(id="dev-1", name="agweb.local", facts={})
    observed = ObservedFacts(hostname="agweb.local", collected_at=datetime(2026, 7, 15, 0, 0, tzinfo=timezone.utc))
    snapshot = make_snapshot(devices=[device], observed=[observed])

    diffs = list(comparators.ingest_lag(snapshot, CONTEXT))

    assert [d.code for d in diffs] == ["ingest_lag"]
    assert diffs[0].actual["nautobot_last_seen"] is None


# --- production_policy -------------------------------------------------------

PROFILES = {"web": {"group": "web_server", "config_schema_version": "1", "variables": {}}}


def test_production_policy_skipped_when_no_profiles_configured():
    node = DesiredNode(id="n1", slug="agweb", name="agweb", lifecycle="active", node_type="device")
    snapshot = make_snapshot(nodes=[node])
    context = DriftContext(generated_at="2026-07-15T12:00:00+00:00", profiles={})

    assert list(comparators.production_policy(snapshot, context)) == []


def test_production_policy_reports_skip_reasons_from_composer():
    node = DesiredNode(id="n1", slug="agweb", name="agweb", lifecycle="active", node_type="device")
    op_config = DesiredNodeOperationalConfig(
        id="op1", node_id="n1", actual_state_policy="required", connection_path="local", expected_host_os="linux"
    )
    snapshot = make_snapshot(nodes=[node], operational_configs=[op_config])
    context = DriftContext(generated_at="2026-07-15T12:00:00+00:00", profiles=PROFILES)

    diffs = list(comparators.production_policy(snapshot, context))

    assert [d.code for d in diffs] == ["no_realized_device"]
    assert diffs[0].severity.value == "error"
    assert diffs[0].target.slug == "agweb"


def test_production_policy_reports_os_mismatch_drift_as_warning():
    node = DesiredNode(id="n1", slug="agmac", name="agmac", lifecycle="active", node_type="device", realized_device_id="dev-1")
    op_config = DesiredNodeOperationalConfig(
        id="op1", node_id="n1", actual_state_policy="required", connection_path="local", expected_host_os="linux"
    )
    device = ActualDevice(
        id="dev-1",
        name="agmac.local",
        facts={"host_system": "Darwin", "last_seen": "2026-07-15T11:00:00+00:00"},
    )
    snapshot = make_snapshot(nodes=[node], operational_configs=[op_config], devices=[device])
    context = DriftContext(generated_at="2026-07-15T12:00:00+00:00", profiles=PROFILES)

    diffs = list(comparators.production_policy(snapshot, context))

    assert [d.code for d in diffs] == ["desired_actual_os_mismatch"]
    assert diffs[0].severity.value == "warning"
    assert diffs[0].actual == {"observed_host_os": "macos"}


def test_production_policy_global_contract_error_becomes_one_diff():
    # A malformed shared deployment-profile map is a Group A error: it is
    # raised by validate_deployment_profiles before the per-node loop even
    # starts, so it stays global. (A node/placement-owned Group C failure,
    # such as invalid_platform_power, is now node-local -- see
    # test_production_policy_local_error_becomes_node_targeted_diff.)
    node = DesiredNode(id="n1", slug="agweb", name="agweb", lifecycle="active", node_type="device")
    op_config = DesiredNodeOperationalConfig(
        id="op1", node_id="n1", actual_state_policy="required", connection_path="local", expected_host_os="linux"
    )
    device = ActualDevice(id="dev-1", name="agweb.local", facts={"host_system": "Linux", "last_seen": "2026-07-15T11:00:00+00:00"})
    node = DesiredNode(**{**node.model_dump(), "realized_device_id": "dev-1"})
    snapshot = make_snapshot(nodes=[node], operational_configs=[op_config], devices=[device])
    broken_profiles = {"web": {"group": "web_server", "config_schema_version": "1", "variables": "not-an-object"}}
    context = DriftContext(generated_at="2026-07-15T12:00:00+00:00", profiles=broken_profiles)

    diffs = list(comparators.production_policy(snapshot, context))

    assert [d.code for d in diffs] == ["invalid_profile_variables"]
    assert diffs[0].target.kind == "global"


def test_production_policy_local_error_becomes_node_targeted_diff():
    # invalid_platform_power is a Group C code (Phase 1): one node's
    # unsafe platform/power combination skips only that node, and today's
    # generic skip-reason conversion already attributes it to that node
    # rather than a global target.
    node = DesiredNode(id="n1", slug="agweb", name="agweb", lifecycle="active", node_type="device")
    op_config = DesiredNodeOperationalConfig(
        id="op1", node_id="n1", actual_state_policy="required", connection_path="local", expected_host_os="linux", power_control="macos_sleep"
    )
    device = ActualDevice(id="dev-1", name="agweb.local", facts={"host_system": "Linux", "last_seen": "2026-07-15T11:00:00+00:00"})
    node = DesiredNode(**{**node.model_dump(), "realized_device_id": "dev-1"})
    snapshot = make_snapshot(nodes=[node], operational_configs=[op_config], devices=[device])
    context = DriftContext(generated_at="2026-07-15T12:00:00+00:00", profiles=PROFILES)

    diffs = list(comparators.production_policy(snapshot, context))

    assert [d.code for d in diffs] == ["invalid_platform_power"]
    assert diffs[0].target.kind == "node"
    assert diffs[0].target.slug == "agweb"


# --- node_intent_matching / endpoint_intent_matching / service_intent_matching --------


def test_node_intent_matching_flags_unlinked_node_with_no_candidate():
    node = DesiredNode(id="n1", slug="agweb", name="agweb", lifecycle="active", node_type="device")
    snapshot = make_snapshot(nodes=[node])

    diffs = list(comparators.node_intent_matching(snapshot, CONTEXT))

    assert [d.code for d in diffs] == ["missing_actual_node"]
    assert diffs[0].severity.value == "error"
    assert diffs[0].target.slug == "agweb"


def test_node_intent_matching_silent_when_realized_device_resolves_cleanly():
    node = DesiredNode(id="n1", slug="agweb", name="agweb", lifecycle="active", node_type="device", realized_device_id="dev-1")
    device = ActualDevice(id="dev-1", name="agweb.local")
    snapshot = make_snapshot(nodes=[node], devices=[device])

    assert list(comparators.node_intent_matching(snapshot, CONTEXT)) == []


def test_node_intent_matching_flags_serial_mismatch_as_conflict():
    node = DesiredNode(
        id="n1", slug="agweb", name="agweb", lifecycle="active", node_type="device",
        expected_spec={"serial": "EXPECTED"}, realized_device_id="dev-1",
    )
    device = ActualDevice(id="dev-1", name="agweb.local", serial="ACTUAL")
    snapshot = make_snapshot(nodes=[node], devices=[device])

    diffs = list(comparators.node_intent_matching(snapshot, CONTEXT))

    assert [d.code for d in diffs] == ["serial_mismatch"]
    assert diffs[0].severity.value == "error"
    assert diffs[0].desired == {"expected": "EXPECTED"}
    assert diffs[0].actual == {"actual": "ACTUAL"}


def test_endpoint_intent_matching_attributes_gap_to_owning_node_target():
    node = DesiredNode(id="n1", slug="agweb", name="agweb", lifecycle="active", node_type="device")
    endpoint = DesiredEndpoint(
        id="e1", name="primary", endpoint_type="primary", node_id="n1", node_slug="agweb",
        ip_address="192.0.2.10/32", ip_policy="dhcp_reserved", dns_name="agweb.example.test", generate_dnsmasq=True,
    )
    snapshot = make_snapshot(nodes=[node], endpoints=[endpoint])

    diffs = list(comparators.endpoint_intent_matching(snapshot, CONTEXT))

    codes = [d.code for d in diffs]
    assert "missing_interface_candidate" in codes
    assert all(d.target.kind == "node" and d.target.slug == "agweb" for d in diffs)


def test_endpoint_intent_matching_satisfied_endpoint_is_silent():
    from nctl_core.sources.desired import DesiredIPRange

    device = ActualDevice(id="dev-1", name="agweb.local")
    interface = ActualInterface(id="iface-1", name="eth0", mac_address="aa:bb:cc:dd:ee:ff", device_id="dev-1")
    ip = ActualIPAddress(id="ip-1", host="192.0.2.10", mask_length=32)
    node = DesiredNode(id="n1", slug="agweb", name="agweb", lifecycle="active", node_type="device", realized_device_id="dev-1")
    endpoint = DesiredEndpoint(
        id="e1", name="primary", endpoint_type="primary", node_id="n1", node_slug="agweb",
        ip_address="192.0.2.10/32", ip_policy="static", dns_name="agweb.example.test", generate_dnsmasq=True,
        realized_ip_address_id="ip-1",
    )
    static_range = DesiredIPRange(
        id="r1", name="lan", slug="lan", start_address="192.0.2.1", end_address="192.0.2.254",
        range_policy="static_pool", lifecycle="active",
    )
    snapshot = make_snapshot(
        nodes=[node], endpoints=[endpoint], ip_ranges=[static_range], devices=[device], interfaces=[interface], ip_addresses=[ip]
    )

    assert list(comparators.endpoint_intent_matching(snapshot, CONTEXT)) == []


def test_service_intent_matching_flags_unresolved_dependency_as_warning():
    service = DesiredService(
        id="s1", slug="api", name="api", display_name="API", service_type="service", lifecycle="active",
        catalog_namespace="default", catalog_metadata_name="api",
    )
    dependency = DesiredDependency(
        id="d1", source_service_id="s1", dependency_kind="component", namespace="default", name="database",
        raw_ref="component:default/database", dependency_type="component", resolution_status="unresolved",
    )
    snapshot = make_snapshot(services=[service], dependencies=[dependency])

    diffs = list(comparators.service_intent_matching(snapshot, CONTEXT))

    codes = {d.code for d in diffs}
    assert "unresolved_dependency" in codes
    assert "service_has_no_active_placement" in codes
    unresolved = next(d for d in diffs if d.code == "unresolved_dependency")
    assert unresolved.severity.value == "warning"
    assert unresolved.target.kind == "service"
    assert unresolved.target.slug == "api"
    no_placement = next(d for d in diffs if d.code == "service_has_no_active_placement")
    assert no_placement.severity.value == "warning"


def test_service_intent_matching_emits_placement_evidence_and_distinct_code():
    service = DesiredService(
        id="s1", slug="nomad", name="nomad", display_name="Nomad", service_type="service",
        lifecycle="active", catalog_namespace="default", catalog_metadata_name="nomad",
    )
    node = DesiredNode(
        id="n1", slug="node-a", name="node-a", lifecycle="active", node_type="device",
        realized_device_id="d1",
    )
    placement = DesiredServicePlacement(
        id="p1", service_id="s1", node_id="n1", instance_name="main",
        deployment_profile="nomad_server", config_schema_version="v1",
    )
    operational = DesiredNodeOperationalConfig(
        id="oc1", node_id="n1", actual_state_policy="observed", expected_host_os="linux",
        connection_path="local",
    )
    device = ActualDevice(
        id="d1", name="node-a",
        facts={
            "host_system": "Linux",
            "service_inventory_updated_at": "2026-07-15T11:30:00+00:00",
            "observed_services": {"nomad": {"state": "failed", "source": "systemd"}},
        },
    )
    snapshot = make_snapshot(
        nodes=[node], services=[service], placements=[placement],
        operational_configs=[operational], devices=[device],
    )

    diffs = list(comparators.service_intent_matching(snapshot, CONTEXT))

    stopped = next(diff for diff in diffs if diff.code == "service_not_running")
    assert stopped.severity.value == "error"
    assert stopped.desired["expected"]["placement_id"] == "p1"
    assert stopped.desired["expected"]["deployment_profile"] == "nomad_server"
    assert stopped.actual["actual"]["observed_state"] == "failed"
    assert stopped.actual["actual"]["observed_source"] == "systemd"


def test_production_policy_local_error_yields_structured_error_not_generic_skip():
    # unknown_profile is a structured Group C error (Phase 1): production_policy
    # must emit the precise error-derived diff and must not also emit a second,
    # generic "production composition skipped this node" diff for the same
    # (node, code) pair.
    node = DesiredNode(id="n1", slug="agbad", name="agbad", lifecycle="active", node_type="device", realized_device_id="dev-1")
    op_config = DesiredNodeOperationalConfig(
        id="op1", node_id="n1", actual_state_policy="required", connection_path="local", expected_host_os="linux"
    )
    device = ActualDevice(id="dev-1", name="agbad.local", facts={"host_system": "Linux", "last_seen": "2026-07-15T11:00:00+00:00"})
    placement = DesiredServicePlacement(
        id="p1", service_id="s1", node_id="n1", instance_name="primary",
        deployment_profile="missing-profile", config_schema_version="1", config={"x": 1},
    )
    snapshot = make_snapshot(nodes=[node], operational_configs=[op_config], devices=[device], placements=[placement])
    context = DriftContext(generated_at="2026-07-15T12:00:00+00:00", profiles=PROFILES)

    diffs = [d for d in comparators.production_policy(snapshot, context) if d.target.slug == "agbad"]

    assert [d.code for d in diffs] == ["unknown_profile"]
    assert diffs[0].severity.value == "error"
    assert diffs[0].target.kind == "node"
    assert diffs[0].desired["placement"]["id"] == "p1"
    assert diffs[0].desired["placement"]["config"] == {"x": 1}
    assert diffs[0].actual == {"stage": "placement_config"}


def test_production_policy_active_placement_not_applied_is_warning_and_converged_safe():
    node = DesiredNode(id="n1", slug="agplanned", name="agplanned", lifecycle="planned", node_type="device")
    op_config = DesiredNodeOperationalConfig(
        id="op1", node_id="n1", actual_state_policy="required", connection_path="local", expected_host_os="linux"
    )
    placement = DesiredServicePlacement(
        id="p1", service_id="s1", node_id="n1", instance_name="primary",
        deployment_profile="web", config_schema_version="1", config={},
    )
    snapshot = make_snapshot(nodes=[node], operational_configs=[op_config], placements=[placement])
    context = DriftContext(generated_at="2026-07-15T12:00:00+00:00", profiles=PROFILES)

    diffs = list(comparators.production_policy(snapshot, context))

    assert [d.code for d in diffs] == ["active_placement_not_applied"]
    assert diffs[0].severity.value == "warning"
    assert diffs[0].target.kind == "node"
    assert diffs[0].target.slug == "agplanned"
    assert diffs[0].desired["placement"]["config"] == {}
    assert diffs[0].actual["node_lifecycle"] == "planned"
    assert diffs[0].actual["application_status"] == "not_applied"


def test_production_policy_active_placement_not_applied_survives_empty_profiles():
    # Decision 4: the lifecycle gate must not depend on loading deployment
    # profiles -- an empty/unreadable profile map degrades the rest of
    # production_policy but must not hide recorded, unapplied intent.
    node = DesiredNode(id="n1", slug="agplanned", name="agplanned", lifecycle="deprecated", node_type="device")
    placement = DesiredServicePlacement(
        id="p1", service_id="s1", node_id="n1", instance_name="primary",
        deployment_profile="web", config_schema_version="1", config={"enabled": True},
    )
    snapshot = make_snapshot(nodes=[node], placements=[placement])
    context = DriftContext(generated_at="2026-07-15T12:00:00+00:00", profiles={})

    diffs = list(comparators.production_policy(snapshot, context))

    assert [d.code for d in diffs] == ["active_placement_not_applied"]
    assert diffs[0].severity.value == "warning"


def test_drift_entry_dispatch_rejects_unknown_composer_drift_code():
    import pytest

    with pytest.raises(AssertionError):
        comparators._drift_entry_diff({"code": "some_future_composer_drift_code", "desired_node_slug": "agx"})
