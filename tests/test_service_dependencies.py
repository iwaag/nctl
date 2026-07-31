from nctl_core.production.derivation import EndpointCandidate
from nctl_core.production.model import BindingInput, NodeInput, PlacementInput
from nctl_core.production.service_dependencies import resolve_service_dependencies


def _node(node_id, slug, *, endpoints=(), placements=()):
    return NodeInput(
        id=node_id, slug=slug, name=slug, lifecycle="active", node_type="device",
        endpoints=tuple(endpoints), placements=tuple(placements),
    )


def _placement(placement_id, service_slug, *, profile, endpoint_id=None, bindings=(), desired_state="active"):
    return PlacementInput(
        id=placement_id, instance_name=placement_id, deployment_profile=profile,
        config_schema_version="1", service_id=f"service-{service_slug}", service_slug=service_slug,
        endpoint_id=endpoint_id, bindings=tuple(bindings), desired_state=desired_state,
    )


def _binding(binding_id, name, provider_slug):
    return BindingInput(
        id=binding_id, binding_name=name,
        provider_service_id=f"service-{provider_slug}", provider_service_slug=provider_slug,
    )


def _ollama_provider(node_id="provider", slug="agstudio", *, endpoint_id="ollama-endpoint"):
    return _node(
        node_id, slug,
        endpoints=[EndpointCandidate(endpoint_id, "ollama", "primary", slug, dns_name=f"{slug}.local", protocol="http", port=11434)],
        placements=[_placement("ollama-main", "ollama", profile="ollama", endpoint_id=endpoint_id)],
    )


def _agent_consumer(bindings):
    return _node(
        "consumer", "aghub",
        placements=[_placement("agent", "node-agent", profile="node_agent", bindings=bindings)],
    )


def test_resolves_node_agent_binding_to_provider_endpoint_url():
    provider = _ollama_provider()
    consumer = _agent_consumer([_binding("b1", "llm_provider", "ollama")])

    resolution = resolve_service_dependencies([consumer, provider])["consumer"]

    assert resolution.error_code is None
    assert resolution.variables == {"nintent_opencode_ollama_url": "http://agstudio.local:11434/v1"}
    assert resolution.provenance == [{
        "consumer_placement_id": "agent", "binding_name": "llm_provider",
        "provider_service_slug": "ollama",
        "provider_placement_id": "ollama-main", "endpoint_id": "ollama-endpoint",
    }]


def test_nodes_without_bindings_produce_no_resolution_entry():
    provider = _ollama_provider()
    consumer = _node("plain", "agplain", placements=[_placement("agent", "node-agent", profile="node_agent")])

    assert resolve_service_dependencies([consumer, provider]) == {}


def test_inactive_consumer_placement_bindings_are_ignored():
    provider = _ollama_provider()
    consumer = _node(
        "consumer", "aghub",
        placements=[_placement(
            "agent", "node-agent", profile="node_agent",
            bindings=[_binding("b1", "llm_provider", "ollama")], desired_state="retired",
        )],
    )

    assert resolve_service_dependencies([consumer, provider]) == {}


def test_missing_provider_is_a_classified_resolution_error():
    consumer = _agent_consumer([_binding("b1", "llm_provider", "ollama")])

    resolution = resolve_service_dependencies([consumer])["consumer"]

    assert resolution.error_code == "binding_provider_missing"
    assert resolution.error_evidence["provider_service_slug"] == "ollama"


def test_multiple_active_provider_placements_are_ambiguous():
    providers = [
        _node("one", "agone",
              endpoints=[EndpointCandidate("one", "ollama-one", "primary", "agone", dns_name="agone.local", protocol="http", port=11434)],
              placements=[_placement("one-placement", "ollama", profile="ollama", endpoint_id="one")]),
        _node("two", "agtwo",
              endpoints=[EndpointCandidate("two", "ollama-two", "primary", "agtwo", dns_name="agtwo.local", protocol="http", port=11434)],
              placements=[_placement("two-placement", "ollama", profile="ollama", endpoint_id="two")]),
    ]
    consumer = _agent_consumer([_binding("b1", "llm_provider", "ollama")])

    resolution = resolve_service_dependencies([consumer, *providers])["consumer"]

    assert resolution.error_code == "binding_provider_ambiguous"
    assert resolution.error_evidence["provider_placement_ids"] == ["one-placement", "two-placement"]


def test_provider_endpoint_missing_is_classified():
    provider = _node("provider", "agstudio", placements=[_placement("ollama-main", "ollama", profile="ollama")])
    consumer = _agent_consumer([_binding("b1", "llm_provider", "ollama")])

    resolution = resolve_service_dependencies([consumer, provider])["consumer"]

    assert resolution.error_code == "binding_endpoint_missing"


def test_provider_endpoint_on_another_node_is_invalid():
    stranger = _node(
        "stranger", "agother",
        endpoints=[EndpointCandidate("stray", "ollama", "primary", "agother", dns_name="agother.local", protocol="http", port=11434)],
    )
    provider = _node(
        "provider", "agstudio",
        placements=[_placement("ollama-main", "ollama", profile="ollama", endpoint_id="stray")],
    )
    consumer = _agent_consumer([_binding("b1", "llm_provider", "ollama")])

    resolution = resolve_service_dependencies([consumer, provider, stranger])["consumer"]

    assert resolution.error_code == "binding_endpoint_invalid"


def test_provider_endpoint_without_port_is_unusable():
    provider = _node(
        "provider", "agstudio",
        endpoints=[EndpointCandidate("ollama-endpoint", "ollama", "primary", "agstudio", dns_name="agstudio.local", protocol="http", port=None)],
        placements=[_placement("ollama-main", "ollama", profile="ollama", endpoint_id="ollama-endpoint")],
    )
    consumer = _agent_consumer([_binding("b1", "llm_provider", "ollama")])

    resolution = resolve_service_dependencies([consumer, provider])["consumer"]

    assert resolution.error_code == "binding_endpoint_unusable"


def test_undeclared_binding_name_is_classified_not_a_crash():
    provider = _ollama_provider()
    consumer = _agent_consumer([_binding("b1", "gpu_provider", "ollama")])

    resolution = resolve_service_dependencies([consumer, provider])["consumer"]

    assert resolution.error_code == "binding_name_undeclared"
    assert resolution.error_evidence["binding_name"] == "gpu_provider"


def test_self_referencing_binding_is_classified():
    consumer = _node(
        "consumer", "aghub",
        placements=[_placement(
            "agent", "node-agent", profile="node_agent",
            bindings=[_binding("b1", "llm_provider", "node-agent")],
        )],
    )

    resolution = resolve_service_dependencies([consumer])["consumer"]

    assert resolution.error_code == "binding_self_reference"


def test_provider_cycle_is_classified():
    # aghub's node-agent binds to ollama; ollama's placement binds back to
    # node-agent: a stored-state cycle nintent would refuse, classified here.
    provider = _node(
        "provider", "agstudio",
        endpoints=[EndpointCandidate("ollama-endpoint", "ollama", "primary", "agstudio", dns_name="agstudio.local", protocol="http", port=11434)],
        placements=[_placement(
            "ollama-main", "ollama", profile="ollama", endpoint_id="ollama-endpoint",
            bindings=[_binding("back", "llm_provider", "node-agent")],
        )],
    )
    consumer = _agent_consumer([_binding("b1", "llm_provider", "ollama")])

    resolution = resolve_service_dependencies([consumer, provider])["consumer"]

    assert resolution.error_code == "binding_cycle"


def test_multiple_bindings_merge_variables_and_provenance():
    provider = _ollama_provider()
    second_provider = _node(
        "provider2", "agsecond",
        endpoints=[EndpointCandidate("second-endpoint", "second", "primary", "agsecond", dns_name="agsecond.local", protocol="http", port=8080)],
        placements=[_placement("second-main", "second-service", profile="ollama", endpoint_id="second-endpoint")],
    )
    consumer = _agent_consumer([
        _binding("b1", "llm_provider", "ollama"),
        _binding("b2", "second_provider", "second-service"),
    ])

    from nctl_core.production import service_dependencies

    original = dict(service_dependencies.PROFILE_BINDING_VARIABLES)
    service_dependencies.PROFILE_BINDING_VARIABLES[("node_agent", "second_provider")] = "nintent_second_url"
    try:
        resolution = resolve_service_dependencies([consumer, provider, second_provider])["consumer"]
    finally:
        service_dependencies.PROFILE_BINDING_VARIABLES.clear()
        service_dependencies.PROFILE_BINDING_VARIABLES.update(original)

    assert resolution.error_code is None
    assert resolution.variables == {
        "nintent_opencode_ollama_url": "http://agstudio.local:11434/v1",
        "nintent_second_url": "http://agsecond.local:8080/v1",
    }
    assert [entry["binding_name"] for entry in resolution.provenance] == ["llm_provider", "second_provider"]


def test_reverse_service_bindings_lists_inbound_consumers_per_provider():
    from nctl_core.production.service_dependencies import reverse_service_bindings

    provider = _ollama_provider()
    consumer_one = _agent_consumer([_binding("b1", "llm_provider", "ollama")])
    consumer_two = _node(
        "consumer2", "agpc",
        placements=[_placement("agent2", "node-agent", profile="node_agent", bindings=[_binding("b2", "llm_provider", "ollama")])],
    )

    reverse = reverse_service_bindings([consumer_one, consumer_two, provider])

    assert reverse["service-ollama"] == [
        {"consumer_node": "aghub", "consumer_service": "node-agent", "binding_name": "llm_provider"},
        {"consumer_node": "agpc", "consumer_service": "node-agent", "binding_name": "llm_provider"},
    ]


def test_reverse_service_bindings_ignores_inactive_consumer_placements():
    from nctl_core.production.service_dependencies import reverse_service_bindings

    provider = _ollama_provider()
    consumer = _node(
        "consumer", "aghub",
        placements=[_placement(
            "agent", "node-agent", profile="node_agent",
            bindings=[_binding("b1", "llm_provider", "ollama")], desired_state="retired",
        )],
    )

    assert reverse_service_bindings([consumer, provider]) == {}
