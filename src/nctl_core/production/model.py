"""Typed inputs and outcomes for deterministic production composition."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from nctl_core.sources.actual import ActualFacts

from .derivation import EffectiveOperationalValues, EndpointCandidate, OperationalOverride


@dataclass(frozen=True)
class PlacementInput:
    id: str
    instance_name: str
    deployment_profile: str
    config_schema_version: str
    desired_state: str = "active"
    config: Mapping[str, Any] = field(default_factory=dict)
    service_id: str = ""
    service_slug: str = ""
    endpoint_id: str | None = None


@dataclass(frozen=True)
class RealizedState:
    realized_type: str | None
    facts: ActualFacts
    nautobot_device_id: str | None = None


@dataclass(frozen=True)
class NodeInput:
    id: str
    slug: str
    name: str
    lifecycle: str
    node_type: str
    role: str | None = None
    accepted_actual_types: tuple[str, ...] = ()
    endpoints: tuple[EndpointCandidate, ...] = ()
    operational_override: OperationalOverride | None = None
    placements: tuple[PlacementInput, ...] = ()
    realized: RealizedState | None = None
    awaiting_manual_initial_access: bool = False


@dataclass(frozen=True)
class ResolvedSshTarget:
    slug: str
    desired_node_id: str
    alias: str
    route: str
    port: int
    generation_id: str


@dataclass
class NodeOutcome:
    state: str
    reasons: list[str]
    effective: EffectiveOperationalValues | None
    finding: dict[str, Any] | None
    active_placement_ids: list[str]
    host_os: str | None = None
    nautobot_device_id: str | None = None
    local_error: Any | None = None
    resolved_route: str | None = None
    resolved_port: int | None = None
    service_dependencies: list[dict[str, str]] = field(default_factory=list)
