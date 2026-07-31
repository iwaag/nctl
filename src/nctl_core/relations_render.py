"""`nctl relations`: the inspection projection (service_relation Phase 4, idea-A §9).

One more read like `drift`/`actual` -- derived fresh from the same desired +
actual state every invocation, never persisted, never cached (roadmap hard
rule 1). Reuses `drift_render.fetch_and_compute_drift` verbatim (relations is
"drift's inputs, projected differently") and the exact same evaluation
primitives drift uses (`evaluate_binding_state`, `normalize_endpoint_url`) so
the two commands can never disagree about a binding's state.

Deliberate difference from `nctl drift`: drift skips bindings whose desired
resolution errored (they already surface as node-local production-composition
drift, see `evaluation_snapshot._binding_checks_by_placement_id`). Relations
includes those edges, carrying the resolver's `error_code` as the edge's own
gap code and no actual-state evidence -- idea-A §9 wants desired-resolution
gaps visible in this projection even though drift folds them elsewhere.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel

from nctl_core.config import Config
from nctl_core.drift.binding_evaluation import BindingCheck, evaluate_binding_state
from nctl_core.drift.engine import DriftResult
from nctl_core.output import Envelope, EnvelopeError
from nctl_core.production.adapter import build_production_node_inputs
from nctl_core.production.model import NodeInput
from nctl_core.production.service_dependencies import (
    ServiceDependencyResolution,
    resolve_all_bindings,
    reverse_service_bindings,
)
from nctl_core.sources.snapshot import SourceSnapshot

from .drift_render import fetch_and_compute_drift

RELATIONS_SCHEMA = "nctl.relations.v1"


class RelationsConsumer(BaseModel):
    node: str
    service: str
    placement_id: str


class RelationsProvider(BaseModel):
    service: str | None = None
    placement_id: str | None = None
    node: str | None = None
    endpoint: str | None = None
    url: str | None = None


class RelationsEdge(BaseModel):
    consumer: RelationsConsumer
    binding_name: str
    provider: RelationsProvider | None = None
    state: str | None = None
    gap_codes: list[str] = []
    evidence: dict[str, Any] = {}


class RelationsData(BaseModel):
    generated_at: str = ""
    edges: list[RelationsEdge] = []
    unreferenced: list[str] = []
    summary: dict[str, int] = {}


def build_relations(cfg: Config, *, host: str | None = None, service: str | None = None) -> Envelope[RelationsData]:
    fetched = fetch_and_compute_drift(cfg)
    if isinstance(fetched, EnvelopeError):
        return Envelope.build(RELATIONS_SCHEMA, RelationsData(), [fetched])
    snapshot, result, generated_at = fetched
    data = render_relations_data(snapshot, result, generated_at, host=host, service=service)
    return Envelope.build(RELATIONS_SCHEMA, data, [])


def render_relations_data(
    snapshot: SourceSnapshot,
    result: DriftResult,
    generated_at: str,
    *,
    host: str | None = None,
    service: str | None = None,
    stale_after_hours: int = 24,
) -> RelationsData:
    """Pure projection: takes a `SourceSnapshot` plus its already-computed
    `DriftResult` (both from `fetch_and_compute_drift`) and derives the edge
    list and `unreferenced` list. Never touches Nautobot itself."""

    node_inputs = build_production_node_inputs(snapshot)
    services_by_id = {s.id: s for s in snapshot.desired.services}
    node_slug_by_placement_id = {
        placement.id: node.slug for node in node_inputs for placement in node.placements
    }
    endpoint_name_by_id = {
        endpoint.id: endpoint.name for node in node_inputs for endpoint in node.endpoints
    }
    provider_converged_by_service_id = {
        target_status.target.id: target_status.status.value == "converged"
        for target_status in result.targets
        if target_status.target.kind == "service" and target_status.target.id
    }
    devices_by_id = {device.id: device for device in snapshot.actual.devices}
    now = datetime.fromisoformat(generated_at.replace("Z", "+00:00")) if generated_at else datetime.now(timezone.utc)
    resolutions_by_placement = resolve_all_bindings(node_inputs)

    edges: list[RelationsEdge] = []
    for consumer in node_inputs:
        for placement in consumer.placements:
            if placement.desired_state != "active":
                continue
            resolutions = resolutions_by_placement.get(placement.id, {})
            for binding_name in sorted(b.binding_name for b in placement.bindings):
                resolution = resolutions.get(binding_name)
                edge = _build_edge(
                    consumer=consumer,
                    placement=placement,
                    binding_name=binding_name,
                    resolution=resolution,
                    services_by_id=services_by_id,
                    node_slug_by_placement_id=node_slug_by_placement_id,
                    endpoint_name_by_id=endpoint_name_by_id,
                    provider_converged_by_service_id=provider_converged_by_service_id,
                    devices_by_id=devices_by_id,
                    now=now,
                    stale_after_hours=stale_after_hours,
                )
                edges.append(edge)

    edges.sort(key=lambda e: (e.consumer.node, e.consumer.service, e.binding_name))
    edges = _filter_edges(edges, host=host, service=service)

    reverse = reverse_service_bindings(node_inputs)
    active_service_ids = {
        placement.service_id
        for consumer in node_inputs
        for placement in consumer.placements
        if placement.desired_state == "active" and placement.service_id
    }
    unreferenced = sorted(
        service.slug
        for service in snapshot.desired.services
        if service.id in active_service_ids and service.id not in reverse
    )

    return RelationsData(
        generated_at=generated_at,
        edges=edges,
        unreferenced=unreferenced,
        summary=_summary(edges),
    )


def _build_edge(
    *,
    consumer: NodeInput,
    placement: Any,
    binding_name: str,
    resolution: ServiceDependencyResolution,
    services_by_id: dict[str, Any],
    node_slug_by_placement_id: dict[str, str],
    endpoint_name_by_id: dict[str, str],
    provider_converged_by_service_id: dict[str, bool],
    devices_by_id: dict[str, Any],
    now: datetime,
    stale_after_hours: int,
) -> RelationsEdge:
    consumer_edge = RelationsConsumer(node=consumer.slug, service=placement.service_slug, placement_id=placement.id)

    if resolution.error_code is not None:
        evidence = dict(resolution.error_evidence or {})
        if resolution.error_message:
            evidence["message"] = resolution.error_message
        return RelationsEdge(
            consumer=consumer_edge, binding_name=binding_name, provider=None, state=None,
            gap_codes=[resolution.error_code], evidence=evidence,
        )

    provenance = resolution.provenance[0]
    provider_placement_id = provenance["provider_placement_id"]
    provider_service_slug = provenance["provider_service_slug"]
    provider_node_slug = node_slug_by_placement_id.get(provider_placement_id)
    provider_service_id = _find_provider_service_id(services_by_id, provider_service_slug)
    desired_url = next(iter(resolution.variables.values()))

    provider = RelationsProvider(
        service=provider_service_slug,
        placement_id=provider_placement_id,
        node=provider_node_slug,
        endpoint=endpoint_name_by_id.get(provenance.get("endpoint_id", "")),
        url=desired_url,
    )

    observed_key = _observed_key(services_by_id, placement.service_id, placement.service_slug)
    device = devices_by_id.get(consumer.realized.nautobot_device_id) if consumer.realized else None
    facts = device.actual_facts() if device is not None else None
    observed_services = facts.observed_services if facts is not None else None
    entry = observed_services.get(observed_key) if isinstance(observed_services, dict) else None
    observed_bindings = entry.get("bindings") if isinstance(entry, dict) else None
    observed = observed_bindings.get(binding_name) if isinstance(observed_bindings, dict) else None
    observed = observed if isinstance(observed, dict) else {}

    check = BindingCheck(
        desired_url=desired_url,
        provider_converged=bool(provider_converged_by_service_id.get(provider_service_id, False)),
    )
    evaluation = evaluate_binding_state(
        binding_name=binding_name,
        check=check,
        configuration_status=observed.get("configuration_status"),
        configured_endpoint=observed.get("configured_endpoint"),
        reachability_status=observed.get("reachability_status"),
        checked_at=observed.get("checked_at"),
        now=now,
        stale_after_hours=stale_after_hours,
    )

    return RelationsEdge(
        consumer=consumer_edge,
        binding_name=binding_name,
        provider=provider,
        state=evaluation.state,
        gap_codes=[evaluation.gap_code] if evaluation.gap_code else [],
        evidence=evaluation.evidence,
    )


def _find_provider_service_id(services_by_id: dict[str, Any], provider_service_slug: str) -> str | None:
    for service_id, service in services_by_id.items():
        if service.slug == provider_service_slug:
            return service_id
    return None


def _observed_key(services_by_id: dict[str, Any], service_id: str, fallback_slug: str) -> str:
    service = services_by_id.get(service_id)
    return service.name if service is not None else fallback_slug


def _filter_edges(edges: list[RelationsEdge], *, host: str | None, service: str | None) -> list[RelationsEdge]:
    if host is not None:
        edges = [e for e in edges if e.consumer.node == host or (e.provider and e.provider.node == host)]
    if service is not None:
        edges = [e for e in edges if e.consumer.service == service or (e.provider and e.provider.service == service)]
    return edges


def _summary(edges: list[RelationsEdge]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for edge in edges:
        key = edge.state or "resolution_error"
        summary[key] = summary.get(key, 0) + 1
    return summary


def render_relations_text(envelope: Envelope[RelationsData]) -> str:
    if not envelope.ok:
        return "\n".join(f"error [{err.code}]: {err.message}" for err in envelope.errors)

    data = envelope.data
    lines: list[str] = []
    for edge in data.edges:
        if edge.provider is not None:
            age = edge.evidence.get("age_hours")
            age_text = f"{age:.1f}h" if isinstance(age, (int, float)) else "?"
            lines.append(
                f"{edge.consumer.node}/{edge.consumer.service} —{edge.binding_name}→ "
                f"{edge.provider.service} @{edge.provider.node} [{edge.state}, {age_text}]"
            )
        else:
            codes = ",".join(edge.gap_codes) or "?"
            lines.append(
                f"{edge.consumer.node}/{edge.consumer.service} —{edge.binding_name}→ "
                f"? [resolution_error: {codes}]"
            )

    if data.unreferenced:
        lines.append("unreferenced (informational): " + ", ".join(data.unreferenced))

    summary_line = " ".join(f"{state}={count}" for state, count in sorted(data.summary.items()))
    lines.append(f"summary: {summary_line}" if summary_line else "summary: (no edges)")
    return "\n".join(lines)
