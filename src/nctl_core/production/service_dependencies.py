"""Resolve service-to-service client endpoints for generated inventory.

Service dependency declarations live in a consumer placement's ``config``.
This first version intentionally uses the small convention
``llm_provider_service: <service slug>`` for the ``node_agent`` profile.  The
resolver is pure and deterministic so the same desired snapshot always
produces the same host variables or the same classified error.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
from typing import Any, Iterable

from .derivation import EndpointCandidate
from .model import NodeInput, PlacementInput


@dataclass(frozen=True)
class ServiceDependencyResolution:
    variables: dict[str, Any]
    provenance: list[dict[str, str]]
    error_code: str | None = None
    error_message: str | None = None
    error_evidence: dict[str, Any] | None = None


def resolve_service_dependencies(nodes: Iterable[NodeInput]) -> dict[str, ServiceDependencyResolution]:
    """Resolve each active node-agent provider dependency from desired state.

    A service may have one active endpoint-bearing placement.  If a provider
    has several active placements, one may declare ``primary: true`` in its
    placement config; otherwise the ambiguity is an intentional, actionable
    composition error rather than an arbitrary topology choice.
    """

    all_nodes = tuple(nodes)
    endpoints = {endpoint.id: (node, endpoint) for node in all_nodes for endpoint in node.endpoints}
    providers: dict[str, list[tuple[NodeInput, PlacementInput]]] = {}
    for node in all_nodes:
        for placement in node.placements:
            if placement.desired_state == "active" and placement.service_slug:
                providers.setdefault(placement.service_slug, []).append((node, placement))

    result: dict[str, ServiceDependencyResolution] = {}
    for consumer in all_nodes:
        dependencies = [
            placement for placement in consumer.placements
            if placement.desired_state == "active"
            and placement.deployment_profile == "node_agent"
            and isinstance(placement.config.get("llm_provider_service"), str)
            and placement.config["llm_provider_service"].strip()
        ]
        if not dependencies:
            continue
        if len(dependencies) > 1:
            result[consumer.id] = _error(
                "ambiguous_llm_provider_dependency",
                "more than one active node-agent placement declares an LLM provider",
                {"consumer_node": consumer.slug, "placement_ids": sorted(item.id for item in dependencies)},
            )
            continue
        dependency = dependencies[0]
        service_slug = str(dependency.config["llm_provider_service"]).strip()
        candidates = providers.get(service_slug, [])
        primary = [(node, placement) for node, placement in candidates if placement.config.get("primary") is True]
        if primary:
            candidates = primary
        if not candidates:
            result[consumer.id] = _error(
                "llm_provider_missing",
                f"no active placement exists for LLM provider service {service_slug!r}",
                {"consumer_placement_id": dependency.id, "service_slug": service_slug},
            )
            continue
        if len(candidates) != 1:
            result[consumer.id] = _error(
                "llm_provider_ambiguous",
                f"LLM provider service {service_slug!r} has multiple active placements",
                {"consumer_placement_id": dependency.id, "service_slug": service_slug,
                 "provider_placement_ids": sorted(placement.id for _node, placement in candidates)},
            )
            continue
        provider_node, provider = candidates[0]
        if not provider.endpoint_id:
            result[consumer.id] = _error(
                "llm_provider_endpoint_missing",
                f"LLM provider service {service_slug!r} has no desired endpoint",
                {"provider_placement_id": provider.id, "service_slug": service_slug},
            )
            continue
        endpoint_pair = endpoints.get(provider.endpoint_id)
        if endpoint_pair is None or endpoint_pair[0].id != provider_node.id:
            result[consumer.id] = _error(
                "llm_provider_endpoint_invalid",
                "LLM provider placement references an endpoint outside its node",
                {"provider_placement_id": provider.id, "endpoint_id": provider.endpoint_id},
            )
            continue
        _node, endpoint = endpoint_pair
        url = _endpoint_url(endpoint)
        if url is None:
            result[consumer.id] = _error(
                "llm_provider_endpoint_unusable",
                "LLM provider endpoint requires an address, protocol, and port",
                {"provider_placement_id": provider.id, "endpoint_id": endpoint.id},
            )
            continue
        provenance = [{
            "consumer_placement_id": dependency.id,
            "service_slug": service_slug,
            "provider_placement_id": provider.id,
            "endpoint_id": endpoint.id,
        }]
        result[consumer.id] = ServiceDependencyResolution(
            variables={"nintent_opencode_ollama_url": url}, provenance=provenance
        )
    return result


def _endpoint_url(endpoint: EndpointCandidate) -> str | None:
    address = endpoint.address()
    protocol = str(endpoint.protocol or "").strip().lower()
    if not address or protocol not in {"http", "https"} or not isinstance(endpoint.port, int):
        return None
    if not 1 <= endpoint.port <= 65535:
        return None
    address = address.split("/", 1)[0]
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        pass
    else:
        if parsed.version == 6:
            address = f"[{address}]"
    return f"{protocol}://{address}:{endpoint.port}/v1"


def _error(code: str, message: str, evidence: dict[str, Any]) -> ServiceDependencyResolution:
    return ServiceDependencyResolution({}, [], code, message, evidence)
