from nctl_core.production.derivation import EndpointCandidate
from nctl_core.production.model import NodeInput, PlacementInput
from nctl_core.production.service_dependencies import resolve_service_dependencies


def _node(node_id, slug, *, endpoints=(), placements=()):
    return NodeInput(
        id=node_id, slug=slug, name=slug, lifecycle="active", node_type="device",
        endpoints=tuple(endpoints), placements=tuple(placements),
    )


def _placement(placement_id, service_slug, *, profile, endpoint_id=None, config=None):
    return PlacementInput(
        id=placement_id, instance_name=placement_id, deployment_profile=profile,
        config_schema_version="1", service_id=f"service-{service_slug}", service_slug=service_slug,
        endpoint_id=endpoint_id, config=config or {},
    )


def test_resolves_node_agent_ollama_endpoint_from_active_provider_placement():
    provider = _node(
        "provider", "agstudio",
        endpoints=[EndpointCandidate("ollama-endpoint", "ollama", "primary", "agstudio", dns_name="agstudio.local", protocol="http", port=11434)],
        placements=[_placement("ollama-main", "ollama", profile="ollama", endpoint_id="ollama-endpoint")],
    )
    consumer = _node(
        "consumer", "aghub",
        placements=[_placement("agent", "node-agent", profile="node_agent", config={"llm_provider_service": "ollama"})],
    )

    resolution = resolve_service_dependencies([consumer, provider])["consumer"]

    assert resolution.error_code is None
    assert resolution.variables == {"nintent_opencode_ollama_url": "http://agstudio.local:11434/v1"}
    assert resolution.provenance == [{
        "consumer_placement_id": "agent", "service_slug": "ollama",
        "provider_placement_id": "ollama-main", "endpoint_id": "ollama-endpoint",
    }]


def test_missing_provider_is_a_classified_resolution_error():
    consumer = _node(
        "consumer", "aghub",
        placements=[_placement("agent", "node-agent", profile="node_agent", config={"llm_provider_service": "ollama"})],
    )

    resolution = resolve_service_dependencies([consumer])["consumer"]

    assert resolution.error_code == "llm_provider_missing"


def test_multiple_providers_require_an_explicit_primary():
    endpoints = [
        EndpointCandidate("one", "ollama-one", "primary", "agone", dns_name="agone.local", protocol="http", port=11434),
        EndpointCandidate("two", "ollama-two", "primary", "agtwo", dns_name="agtwo.local", protocol="http", port=11434),
    ]
    providers = [
        _node("one", "agone", endpoints=[endpoints[0]], placements=[_placement("one-placement", "ollama", profile="ollama", endpoint_id="one")]),
        _node("two", "agtwo", endpoints=[endpoints[1]], placements=[_placement("two-placement", "ollama", profile="ollama", endpoint_id="two")]),
    ]
    consumer = _node("consumer", "aghub", placements=[_placement("agent", "node-agent", profile="node_agent", config={"llm_provider_service": "ollama"})])

    resolution = resolve_service_dependencies([consumer, *providers])["consumer"]

    assert resolution.error_code == "llm_provider_ambiguous"
