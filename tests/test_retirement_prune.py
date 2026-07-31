from nctl_core.compute.model import DesiredComputeInstance
from nctl_core.drift.engine import DriftResult, TargetStatus
from nctl_core.drift.model import DiffRecord, Severity, Status, Target
from nctl_core.retirement_prune import _desired_operations, _resolve
from nctl_core.sources.actual import ActualCluster, ActualDevice, ActualSnapshot, ActualVirtualMachine, ProxmoxClusterFacts, ProxmoxVirtualMachineFacts
from nctl_core.sources.desired import (
    DesiredEndpoint,
    DesiredNode,
    DesiredService,
    DesiredServiceBinding,
    DesiredServicePlacement,
    DesiredSnapshot,
)
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


def test_no_surviving_actual_roots_retries_desired_cleanup():
    snapshot = _snapshot().model_copy(update={"actual": ActualSnapshot()})
    eligibility, node, instance, payload = _resolve(snapshot, DriftResult(), "agfixture")
    assert eligibility["result"] == "eligible"
    assert payload is None
    assert eligibility["actual_roots"] == {"device_id": None, "virtual_machine_id": None}
    assert [operation["kind"] for operation in _desired_operations(snapshot, node, instance)] == ["desired_endpoint", "desired_compute_instance", "desired_node"]


def test_eligibility_surfaces_inbound_consumers_of_a_hosted_provider_service():
    base = _snapshot()
    consumer_node = DesiredNode(id="node-2", slug="agconsumer", name="agconsumer", lifecycle="active", node_type="device")
    snapshot = base.model_copy(update={
        "desired": base.desired.model_copy(update={
            "nodes": [*base.desired.nodes, consumer_node],
            "services": [DesiredService(id="svc-ollama", slug="ollama", name="ollama", lifecycle="active")],
            "placements": [
                DesiredServicePlacement(
                    id="ollama-main", service_id="svc-ollama", node_id="node-1",
                    instance_name="ollama-main", deployment_profile="ollama", config_schema_version="1",
                ),
                DesiredServicePlacement(
                    id="agent-placement", service_id="svc-agent", node_id="node-2",
                    instance_name="node-agent", deployment_profile="node_agent", config_schema_version="1",
                ),
            ],
            "service_bindings": [
                DesiredServiceBinding(
                    id="binding-1", binding_name="llm_provider",
                    consumer_placement_id="agent-placement",
                    provider_service_id="svc-ollama", provider_service_slug="ollama",
                )
            ],
        }),
    })
    drift = DriftResult(targets=[TargetStatus(
        target=Target(kind="compute_instance", id=snapshot.desired.compute_instances[0].id, slug="agfixture"),
        status=Status.CONVERGED,
        diffs=[DiffRecord(target=Target(kind="compute_instance", id=snapshot.desired.compute_instances[0].id),
                           code="compute_instance_removal_complete", severity=Severity.INFO, message="done")],
    )])

    eligibility, *_ = _resolve(snapshot, drift, "agfixture")

    assert eligibility["result"] == "eligible"
    assert eligibility["inbound_consumers"] == [
        {"consumer_node": "agconsumer", "consumer_service": "", "binding_name": "llm_provider"}
    ]


def test_partial_actual_cleanup_plans_the_surviving_root():
    base = _snapshot()
    for actual, absent, remaining in (
        (ActualSnapshot(devices=base.actual.devices, clusters=base.actual.clusters), ["virtual_machine"], ["device"]),
        (ActualSnapshot(virtual_machines=base.actual.virtual_machines, clusters=base.actual.clusters), ["device"], ["virtual_machine"]),
    ):
        snapshot = base.model_copy(update={"actual": actual})
        drift = DriftResult()
        if actual.virtual_machines:
            instance = snapshot.desired.compute_instances[0]
            drift = DriftResult(targets=[TargetStatus(target=Target(kind="compute_instance", id=instance.id, slug="agfixture"), status=Status.CONVERGED, diffs=[DiffRecord(target=Target(kind="compute_instance", id=instance.id), code="compute_instance_removal_complete", severity=Severity.INFO, message="done")])])
        eligibility, _node, _instance, payload = _resolve(snapshot, drift, "agfixture")
        assert eligibility["result"] == "eligible"
        assert [name for name, value in eligibility["actual_roots"].items() if value is None] == [f"{name}_id" for name in absent]
        assert [name for name, value in eligibility["actual_roots"].items() if value is not None] == [f"{name}_id" for name in remaining]
        assert payload == {"desired_node_id": "node-1", **eligibility["actual_roots"]}
