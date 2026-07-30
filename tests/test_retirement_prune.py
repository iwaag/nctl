from nctl_core.compute.model import DesiredComputeInstance
from nctl_core.drift.engine import DriftResult, TargetStatus
from nctl_core.drift.model import DiffRecord, Severity, Status, Target
from nctl_core.retirement_prune import _desired_operations, _resolve
from nctl_core.sources.actual import ActualCluster, ActualDevice, ActualSnapshot, ActualVirtualMachine, ProxmoxClusterFacts, ProxmoxVirtualMachineFacts
from nctl_core.sources.desired import DesiredEndpoint, DesiredNode, DesiredSnapshot
from nctl_core.sources.snapshot import SourceSnapshot
from datetime import datetime, timezone


def _snapshot():
    node = DesiredNode(id="node-1", slug="agfixture", name="agfixture", lifecycle="retired", node_type="service_host", realized_device_id="device-1")
    instance = DesiredComputeInstance(id="instance-1", desired_node_id=node.id, platform_id="platform-1", instance_kind="container", desired_presence="absent", vcpus=1, memory_mb=512, root_disk_gb=8, realized_vm_id="vm-1")
    return SourceSnapshot(
        desired=DesiredSnapshot(nodes=[node], endpoints=[DesiredEndpoint(id="endpoint-1", name="primary", endpoint_type="primary", node_id=node.id, node_slug=node.slug)], compute_instances=[instance]),
        actual=ActualSnapshot(devices=[ActualDevice(id="device-1", name="agfixture")], clusters=[ActualCluster(id="cluster-1", name="pve", proxmox=ProxmoxClusterFacts(observation_state="complete"))], virtual_machines=[ActualVirtualMachine(id="vm-1", name="agfixture", cluster_id="cluster-1", proxmox=ProxmoxVirtualMachineFacts(guest_type="lxc", presence="absent"))]),
        fetched_at=datetime.now(timezone.utc),
    )


def test_completed_lxc_resolves_to_exact_roots_and_child_first_desired_deletes():
    snapshot = _snapshot()
    instance = snapshot.desired.compute_instances[0]
    drift = DriftResult(targets=[TargetStatus(target=Target(kind="compute_instance", id=instance.id, slug="agfixture"), status=Status.CONVERGED, diffs=[DiffRecord(target=Target(kind="compute_instance", id=instance.id), code="compute_instance_removal_complete", severity=Severity.INFO, message="done")])])
    eligibility, node, selected, payload = _resolve(snapshot, drift, "agfixture")
    assert eligibility["result"] == "eligible"
    assert payload == {"desired_node_id": "node-1", "device_id": "device-1", "virtual_machine_id": "vm-1"}
    assert [operation["kind"] for operation in _desired_operations(snapshot, node, selected)] == ["desired_endpoint", "desired_compute_instance", "desired_node"]


def test_missing_desired_node_is_a_repeatable_noop():
    snapshot = _snapshot().model_copy(update={"desired": DesiredSnapshot()})
    eligibility, *_ = _resolve(snapshot, DriftResult(), "agfixture")
    assert eligibility["result"] == "already_pruned"


def test_actual_first_partial_progress_retries_only_desired_cleanup():
    snapshot = _snapshot().model_copy(update={"actual": ActualSnapshot()})
    eligibility, node, instance, payload = _resolve(snapshot, DriftResult(), "agfixture")
    assert eligibility["result"] == "actual_already_pruned"
    assert payload is None
    assert eligibility["absent_actual_roots"] == ["virtual_machine", "device"]
    assert eligibility["remaining_actual_roots"] == []
    assert eligibility["actual_deletion_requested"] is False
    assert [operation["kind"] for operation in _desired_operations(snapshot, node, instance)] == ["desired_endpoint", "desired_compute_instance", "desired_node"]


def test_partial_actual_cleanup_reports_exact_roots_and_never_requests_delete():
    base = _snapshot()
    for actual, absent, remaining in (
        (ActualSnapshot(devices=base.actual.devices), ["virtual_machine"], ["device"]),
        (ActualSnapshot(virtual_machines=base.actual.virtual_machines), ["device"], ["virtual_machine"]),
    ):
        snapshot = base.model_copy(update={"actual": actual})
        eligibility, _node, _instance, payload = _resolve(snapshot, DriftResult(), "agfixture")
        assert eligibility["result"] == "actual_already_pruned"
        assert eligibility["absent_actual_roots"] == absent
        assert eligibility["remaining_actual_roots"] == remaining
        assert eligibility["actual_deletion_requested"] is False
        assert payload is None
