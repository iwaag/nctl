from __future__ import annotations

from datetime import datetime, timezone

from nctl_core.drift import comparators
from nctl_core.drift.context import DriftContext
from nctl_core.sources.actual import ActualDevice, ActualSnapshot, ActualVirtualMachine
from nctl_core.sources.desired import DesiredNode, DesiredNodeOperationalConfig, DesiredSnapshot
from nctl_core.sources.observed import ObservedFacts
from nctl_core.sources.snapshot import SourceSnapshot

CONTEXT = DriftContext(generated_at="2026-07-15T12:00:00+00:00")


def make_snapshot(*, nodes=(), operational_configs=(), devices=(), vms=(), observed=()) -> SourceSnapshot:
    return SourceSnapshot(
        desired=DesiredSnapshot(nodes=list(nodes), operational_configs=list(operational_configs)),
        actual=ActualSnapshot(devices=list(devices), virtual_machines=list(vms)),
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
    assert diffs[0].target.kind == "global"
