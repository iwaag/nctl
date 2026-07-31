"""Resolve service-to-service bindings into consumer host variables.

Service dependencies are `DesiredServiceBinding` rows (idea-A §3.1) attached
to a consumer placement, fetched by `sources/desired.py` and threaded onto
`PlacementInput.bindings`. The resolver walks, per binding: provider service →
exactly one active placement → usable endpoint → URL (idea-A §4), and returns
per-consumer-node variables + provenance or one classified §6 error. Pure and
deterministic: the same desired snapshot always produces the same result.

Self-reference and cycles are rejected by nintent's batch validator at write
time, but this resolver reads a snapshot it does not control, so they are
classified errors here rather than assertions.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
from typing import Any, Iterable

from .derivation import EndpointCandidate
from .model import BindingInput, NodeInput, PlacementInput

# The nctl twin of nintent's `PROFILE_BINDING_NAMES`: which binding names a
# profile declares, and which inventory variable each one produces. A binding
# name arriving from desired state that is not declared here is a classified
# error (`binding_name_undeclared`), never a crash.
PROFILE_BINDING_VARIABLES: dict[tuple[str, str], str] = {
    ("node_agent", "llm_provider"): "nintent_opencode_ollama_url",
}


@dataclass(frozen=True)
class ServiceDependencyResolution:
    variables: dict[str, Any]
    provenance: list[dict[str, str]]
    error_code: str | None = None
    error_message: str | None = None
    error_evidence: dict[str, Any] | None = None


def resolve_service_dependencies(nodes: Iterable[NodeInput]) -> dict[str, ServiceDependencyResolution]:
    """Resolve every binding on each node's active placements.

    Returns one entry per consumer node that carries at least one binding:
    either the merged variables/provenance of all its bindings, or the first
    classified error in deterministic (placement instance_name, binding_name)
    order.
    """

    all_nodes = tuple(nodes)
    endpoints = {endpoint.id: (node, endpoint) for node in all_nodes for endpoint in node.endpoints}
    active_by_service: dict[str, list[tuple[NodeInput, PlacementInput]]] = {}
    for node in all_nodes:
        for placement in node.placements:
            if placement.desired_state == "active" and placement.service_id:
                active_by_service.setdefault(placement.service_id, []).append((node, placement))

    result: dict[str, ServiceDependencyResolution] = {}
    for consumer in all_nodes:
        bound = [
            (placement, binding)
            for placement in consumer.placements
            if placement.desired_state == "active"
            for binding in placement.bindings
        ]
        if not bound:
            continue
        variables: dict[str, Any] = {}
        provenance: list[dict[str, str]] = []
        error: ServiceDependencyResolution | None = None
        for placement, binding in bound:
            resolved = _resolve_binding(consumer, placement, binding, active_by_service, endpoints)
            if resolved.error_code is not None:
                error = resolved
                break
            variables.update(resolved.variables)
            provenance.extend(resolved.provenance)
        result[consumer.id] = error if error is not None else ServiceDependencyResolution(variables, provenance)
    return result


def _resolve_binding(
    consumer: NodeInput,
    placement: PlacementInput,
    binding: BindingInput,
    active_by_service: dict[str, list[tuple[NodeInput, PlacementInput]]],
    endpoints: dict[str, tuple[NodeInput, EndpointCandidate]],
) -> ServiceDependencyResolution:
    variable = PROFILE_BINDING_VARIABLES.get((placement.deployment_profile, binding.binding_name))
    if variable is None:
        return _error(
            "binding_name_undeclared",
            f"profile {placement.deployment_profile!r} does not declare binding name {binding.binding_name!r}",
            {"consumer_placement_id": placement.id, "binding_name": binding.binding_name,
             "deployment_profile": placement.deployment_profile},
        )
    if binding.provider_service_id == placement.service_id:
        return _error(
            "binding_self_reference",
            f"binding {binding.binding_name!r} points at its own consumer service",
            {"consumer_placement_id": placement.id, "binding_name": binding.binding_name,
             "provider_service_slug": binding.provider_service_slug},
        )
    candidates = active_by_service.get(binding.provider_service_id, [])
    if not candidates:
        return _error(
            "binding_provider_missing",
            f"no active placement exists for provider service {binding.provider_service_slug!r}",
            {"consumer_placement_id": placement.id, "binding_name": binding.binding_name,
             "provider_service_slug": binding.provider_service_slug},
        )
    if len(candidates) != 1:
        return _error(
            "binding_provider_ambiguous",
            f"provider service {binding.provider_service_slug!r} has multiple active placements",
            {"consumer_placement_id": placement.id, "binding_name": binding.binding_name,
             "provider_service_slug": binding.provider_service_slug,
             "provider_placement_ids": sorted(item.id for _node, item in candidates)},
        )
    if placement.service_id and _reaches(
        binding.provider_service_id, placement.service_id, active_by_service, set()
    ):
        return _error(
            "binding_cycle",
            f"binding {binding.binding_name!r} closes a provider cycle back to service "
            f"{placement.service_slug!r}",
            {"consumer_placement_id": placement.id, "binding_name": binding.binding_name,
             "provider_service_slug": binding.provider_service_slug},
        )
    provider_node, provider = candidates[0]
    if not provider.endpoint_id:
        return _error(
            "binding_endpoint_missing",
            f"provider service {binding.provider_service_slug!r} has no desired endpoint",
            {"provider_placement_id": provider.id, "binding_name": binding.binding_name,
             "provider_service_slug": binding.provider_service_slug},
        )
    endpoint_pair = endpoints.get(provider.endpoint_id)
    if endpoint_pair is None or endpoint_pair[0].id != provider_node.id:
        return _error(
            "binding_endpoint_invalid",
            "provider placement references an endpoint outside its node",
            {"provider_placement_id": provider.id, "endpoint_id": provider.endpoint_id,
             "binding_name": binding.binding_name},
        )
    _node, endpoint = endpoint_pair
    url = _endpoint_url(endpoint)
    if url is None:
        return _error(
            "binding_endpoint_unusable",
            "provider endpoint requires an address, protocol, and port",
            {"provider_placement_id": provider.id, "endpoint_id": endpoint.id,
             "binding_name": binding.binding_name},
        )
    return ServiceDependencyResolution(
        variables={variable: url},
        provenance=[{
            "consumer_placement_id": placement.id,
            "binding_name": binding.binding_name,
            "provider_service_slug": binding.provider_service_slug,
            "provider_placement_id": provider.id,
            "endpoint_id": endpoint.id,
        }],
    )


def _reaches(
    service_id: str,
    target_service_id: str,
    active_by_service: dict[str, list[tuple[NodeInput, PlacementInput]]],
    seen: set[str],
) -> bool:
    """Whether the binding graph reaches `target_service_id` from `service_id`."""

    if service_id == target_service_id:
        return True
    if service_id in seen:
        return False
    seen.add(service_id)
    for _node, placement in active_by_service.get(service_id, ()):
        for binding in placement.bindings:
            if _reaches(binding.provider_service_id, target_service_id, active_by_service, seen):
                return True
    return False


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
