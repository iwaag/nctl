"""Pure-builder tests for `render_relations_data` (service_relation Phase 4 Step 1).

Constructs `SourceSnapshot`/`DriftResult` directly (no GraphQL mocking) since
the builder itself never touches Nautobot -- it is handed an already-fetched
snapshot and an already-computed drift result, mirroring how
`fetch_and_compute_drift` produces both in one call.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from nctl_core.drift.engine import DriftResult, TargetStatus
from nctl_core.drift.model import Status, Target
from nctl_core.relations_render import render_relations_data
from nctl_core.sources.actual import ActualDevice, ActualSnapshot
from nctl_core.sources.desired import (
    DesiredEndpoint,
    DesiredNode,
    DesiredService,
    DesiredServiceBinding,
    DesiredServicePlacement,
    DesiredSnapshot,
)
from nctl_core.sources.snapshot import SourceSnapshot

GENERATED_AT = datetime.now(timezone.utc).isoformat()


def _fresh(hours_ago: float = 0.1) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def _base_desired(*, extra_placements=(), extra_services=(), extra_bindings=()) -> DesiredSnapshot:
    return DesiredSnapshot(
        nodes=[
            DesiredNode(
                id="node-consumer", slug="aghub", name="aghub", lifecycle="active",
                node_type="device", accepted_actual_types=["device"], realized_device_id="dev-consumer",
            ),
            DesiredNode(
                id="node-provider", slug="agstudio", name="agstudio", lifecycle="active",
                node_type="device", accepted_actual_types=["device"], realized_device_id=None,
            ),
        ],
        endpoints=[
            DesiredEndpoint(
                id="ep-ollama", name="ollama-api", endpoint_type="service",
                node_id="node-provider", node_slug="agstudio",
                dns_name="agstudio.home.arpa", protocol="http", port=11434,
            ),
        ],
        placements=[
            DesiredServicePlacement(
                id="placement-consumer", service_id="svc-node-agent", node_id="node-consumer",
                instance_name="node-agent", deployment_profile="node_agent", config_schema_version="1",
            ),
            DesiredServicePlacement(
                id="placement-provider", service_id="svc-ollama", node_id="node-provider",
                endpoint_id="ep-ollama", instance_name="ollama", deployment_profile="ollama",
                config_schema_version="1",
            ),
            *extra_placements,
        ],
        services=[
            DesiredService(id="svc-node-agent", slug="node-agent", name="node-agent", lifecycle="active"),
            DesiredService(id="svc-ollama", slug="ollama", name="ollama", lifecycle="active"),
            *extra_services,
        ],
        service_bindings=[
            DesiredServiceBinding(
                id="binding-1", binding_name="llm_provider", consumer_placement_id="placement-consumer",
                provider_service_id="svc-ollama", provider_service_slug="ollama",
            ),
            *extra_bindings,
        ],
    )


def _device(observed_services: dict | None = None) -> ActualDevice:
    facts = {"observed_services": observed_services} if observed_services is not None else {}
    return ActualDevice(id="dev-consumer", name="aghub.local", facts=facts)


def _drift_result(*, ollama_status: Status = Status.CONVERGED) -> DriftResult:
    return DriftResult(
        targets=[
            TargetStatus(target=Target(kind="service", id="svc-node-agent", slug="node-agent"), status=Status.CONVERGED, diffs=[]),
            TargetStatus(target=Target(kind="service", id="svc-ollama", slug="ollama"), status=ollama_status, diffs=[]),
        ]
    )


def _snapshot(desired: DesiredSnapshot, device: ActualDevice) -> SourceSnapshot:
    return SourceSnapshot(
        desired=desired, actual=ActualSnapshot(devices=[device]), fetched_at=datetime.now(timezone.utc),
    )


def test_satisfied_edge():
    device = _device({
        "node-agent": {
            "state": "active", "source": "systemd", "checked_at": _fresh(),
            "bindings": {
                "llm_provider": {
                    "configuration_status": "present",
                    "configured_endpoint": "http://agstudio.home.arpa:11434/v1",
                    "reachability_status": "reachable",
                    "checked_at": _fresh(),
                },
            },
        },
    })
    snapshot = _snapshot(_base_desired(), device)
    data = render_relations_data(snapshot, _drift_result(), GENERATED_AT)

    assert len(data.edges) == 1
    edge = data.edges[0]
    assert edge.consumer.node == "aghub"
    assert edge.consumer.service == "node-agent"
    assert edge.binding_name == "llm_provider"
    assert edge.provider.service == "ollama"
    assert edge.provider.node == "agstudio"
    assert edge.provider.endpoint == "ollama-api"
    assert edge.provider.url == "http://agstudio.home.arpa:11434/v1"
    assert edge.state == "satisfied"
    assert edge.gap_codes == []
    assert data.summary == {"satisfied": 1}


def test_misbound_edge():
    device = _device({
        "node-agent": {
            "state": "active", "source": "systemd", "checked_at": _fresh(),
            "bindings": {
                "llm_provider": {
                    "configuration_status": "present",
                    "configured_endpoint": "http://wrong-host.example.test:11434/v1",
                    "reachability_status": "reachable",
                    "checked_at": _fresh(),
                },
            },
        },
    })
    snapshot = _snapshot(_base_desired(), device)
    data = render_relations_data(snapshot, _drift_result(), GENERATED_AT)

    edge = data.edges[0]
    assert edge.state == "misbound"
    assert edge.gap_codes == ["binding_misbound"]


def test_resolution_failure_edge_included_with_error_code():
    extra_binding = DesiredServiceBinding(
        id="binding-ambiguous", binding_name="llm_provider", consumer_placement_id="placement-consumer",
        provider_service_id="svc-ollama", provider_service_slug="ollama",
    )
    # Two active placements for the same provider service makes it ambiguous.
    extra_placement = DesiredServicePlacement(
        id="placement-provider-2", service_id="svc-ollama", node_id="node-provider",
        endpoint_id="ep-ollama", instance_name="ollama-2", deployment_profile="ollama",
        config_schema_version="1",
    )
    desired = _base_desired(extra_placements=[extra_placement])
    device = _device({})
    snapshot = _snapshot(desired, device)
    data = render_relations_data(snapshot, _drift_result(), GENERATED_AT)

    assert len(data.edges) == 1
    edge = data.edges[0]
    assert edge.provider is None
    assert edge.state is None
    assert edge.gap_codes == ["binding_provider_ambiguous"]
    assert edge.evidence["provider_service_slug"] == "ollama"
    assert data.summary == {"resolution_error": 1}


def test_provider_not_converged_gap():
    device = _device({
        "node-agent": {
            "state": "active", "source": "systemd", "checked_at": _fresh(),
            "bindings": {
                "llm_provider": {
                    "configuration_status": "present",
                    "configured_endpoint": "http://agstudio.home.arpa:11434/v1",
                    "reachability_status": "reachable",
                    "checked_at": _fresh(),
                },
            },
        },
    })
    snapshot = _snapshot(_base_desired(), device)
    data = render_relations_data(snapshot, _drift_result(ollama_status=Status.DRIFTING), GENERATED_AT)

    edge = data.edges[0]
    assert edge.state == "satisfied"
    assert edge.gap_codes == ["binding_provider_not_converged"]


def test_unreferenced_service_listed_informational():
    extra_service = DesiredService(id="svc-orphan", slug="orphan", name="orphan", lifecycle="active")
    extra_placement = DesiredServicePlacement(
        id="placement-orphan", service_id="svc-orphan", node_id="node-provider",
        instance_name="orphan", deployment_profile="ollama", config_schema_version="1",
    )
    desired = _base_desired(extra_services=[extra_service], extra_placements=[extra_placement])
    device = _device({})
    snapshot = _snapshot(desired, device)
    data = render_relations_data(snapshot, _drift_result(), GENERATED_AT)

    assert data.unreferenced == ["node-agent", "orphan"]


def test_edges_sorted_deterministically():
    device = _device({})
    # No bindings observed -> unknown state, but ordering is what's under test.
    node_a = DesiredNode(id="node-a", slug="aaa", name="aaa", lifecycle="active", node_type="device", realized_device_id="dev-consumer")
    node_b = DesiredNode(id="node-b", slug="zzz", name="zzz", lifecycle="active", node_type="device", realized_device_id="dev-consumer")
    placement_a = DesiredServicePlacement(id="p-a", service_id="svc-node-agent", node_id="node-a", instance_name="node-agent", deployment_profile="node_agent", config_schema_version="1")
    placement_b = DesiredServicePlacement(id="p-b", service_id="svc-node-agent", node_id="node-b", instance_name="node-agent", deployment_profile="node_agent", config_schema_version="1")
    binding_a = DesiredServiceBinding(id="b-a", binding_name="llm_provider", consumer_placement_id="p-a", provider_service_id="svc-ollama", provider_service_slug="ollama")
    binding_b = DesiredServiceBinding(id="b-b", binding_name="llm_provider", consumer_placement_id="p-b", provider_service_id="svc-ollama", provider_service_slug="ollama")
    desired = DesiredSnapshot(
        nodes=[node_a, node_b, DesiredNode(id="node-provider", slug="agstudio", name="agstudio", lifecycle="active", node_type="device")],
        endpoints=[DesiredEndpoint(id="ep-ollama", name="ollama-api", endpoint_type="service", node_id="node-provider", node_slug="agstudio", dns_name="agstudio.home.arpa", protocol="http", port=11434)],
        placements=[
            placement_a, placement_b,
            DesiredServicePlacement(id="placement-provider", service_id="svc-ollama", node_id="node-provider", endpoint_id="ep-ollama", instance_name="ollama", deployment_profile="ollama", config_schema_version="1"),
        ],
        services=[
            DesiredService(id="svc-node-agent", slug="node-agent", name="node-agent", lifecycle="active"),
            DesiredService(id="svc-ollama", slug="ollama", name="ollama", lifecycle="active"),
        ],
        service_bindings=[binding_a, binding_b],
    )
    snapshot = _snapshot(desired, device)
    data = render_relations_data(snapshot, _drift_result(), GENERATED_AT)

    assert [edge.consumer.node for edge in data.edges] == ["aaa", "zzz"]
