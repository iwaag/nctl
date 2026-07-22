"""Deterministic production inventory composition, ported from nintent's
`production_inventory.py` (Phase 2 Step 2).

This module is pure: it has no Django or Nautobot dependency and never
performs inventory-time database access. `nctl_core.production.adapter` is
responsible for reading a `SourceSnapshot` and packaging it into the input
dataclasses below, alongside the validated deployment-profile map from
`nctl_core.production.profiles`. The composer then returns a schema ``1.0``
inventory document plus a structured companion report — byte-identical in
shape to nintent's `ExportProductionInventory` Job output (see p2/report2.md
for the parity check).

`ActualFacts`/`read_actual_facts`/`actual_type_problem`/`missing_required_facts`
already live in `nctl_core.sources.actual` (ported there in Phase 2 Step 1,
since the actual-fact allowlist is also needed by the drift engine); this
module imports them rather than re-porting them a second time.

No value here is inferred from another fact. Service-group membership comes only
from active placements and the deployment-profile map, OS selector groups come
only from the normalized observed system (or an explicit declared platform), and
the actual-state allowlist is whatever `sources.actual` extracted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

import yaml

from nctl_core.sources.actual import ActualFacts
from nctl_core.ssh_trust import build_ansible_ssh_common_args, derive_host_key_alias

from .contract import (
    PRODUCTION_INVENTORY_SCHEMA_VERSION,
    PRODUCTION_REPORT_SCHEMA_VERSION,
    ContractError,
    actual_state_problem,
    map_placement_config,
    merge_host_variables,
    resolve_connection_variables,
    validate_deployment_profiles,
    validate_production_inventory_document,
    validate_production_report_v3,
)
from .derivation import (
    DerivationFailure,
    EffectiveOperationalValues,
    EndpointCandidate,
    OperationalOverride,
    resolve_operational_values,
)

# Production-eligible desired node types.  Containers never enter the production
# inventory; the actual-backed/declared distinction is made by the operational
# config policy, not the desired node type.
PRODUCTION_ELIGIBLE_NODE_TYPES = frozenset({"device", "virtual_machine", "service_host"})
PRODUCTION_ELIGIBLE_LIFECYCLES = frozenset({"approved", "active"})

# Core groups that must always exist in the document, even when empty.
_CORE_GROUPS = ("ssh_hosts", "linux", "macos", "haos", "power_managed")
_OS_SELECTOR_GROUP = {"linux": "linux", "macos": "macos", "haos": "haos"}

# Phase 1 (better_usability p1) target-local failure-scope groups, per
# p0/field-classification.md Section 6 Group C. These are the exhaustively
# audited `ContractError` codes that are owned by one node/placement, and must
# therefore skip only that node rather than aborting the whole composition.
# `reconcile/classify.py` imports these same constants (does not redeclare
# the vocabulary) so composer, comparator, and classifier can never drift
# apart on which codes are local.
NODE_LOCAL_CODES = frozenset(
    {
        "unsupported_observed_host_os",
        "invalid_platform_power",
        "endpoint_node_mismatch",
        "unresolved_connection_path",
        "invalid_connection_address",
    }
)
OPERATIONAL_DERIVATION_CODES = frozenset(
    {"ambiguous_connection_endpoints", "missing_connection_endpoint"}
)
PLACEMENT_LOCAL_CODES = frozenset(
    {
        "unknown_profile",
        "unsupported_config_schema",
        "invalid_placement_config",
        "unknown_config_key",
        "missing_required_config",
        "invalid_profile_value_type",
    }
)
MERGE_LOCAL_CODES = frozenset({"conflicting_host_variable"})

# The full Group C set: every ContractError code caught and localized inside
# the eligible-node loop. An unexpected ContractError code escaping a
# per-node helper is re-raised, not silently downgraded (Decision 2).
LOCAL_COMPOSITION_CODES = NODE_LOCAL_CODES | PLACEMENT_LOCAL_CODES | MERGE_LOCAL_CODES

# The unapplied-intent code (Step 1.3): recorded active intent that cannot
# enter production because its node's lifecycle is out of scope. Not a
# ContractError -- a report drift entry -- but shares the "every Phase 1
# code is manual review" vocabulary with LOCAL_COMPOSITION_CODES.
ACTIVE_PLACEMENT_NOT_APPLIED = "active_placement_not_applied"

# Every node-scoped finding that means production actuation for that node is
# unsafe. Classifier and planner import this one current vocabulary; observation
# codes retain their observation classification while still blocking actuation.
PRODUCTION_BLOCKING_NODE_CODES = (
    LOCAL_COMPOSITION_CODES
    | OPERATIONAL_DERIVATION_CODES
    | {
        ACTIVE_PLACEMENT_NOT_APPLIED,
        "invalid_actual_timestamp",
        "missing_actual_data",
        "missing_mac_address",
        "missing_observed_system",
        "no_realized_device",
        "stale_actual_data",
        "unsupported_actual_type",
    }
)


class LocalCompositionError(Exception):
    """One target-local Group C failure, carrying enough context to become a
    node's `local_findings` entry and `production.state == "skipped"` reason
    (report schema 3.0) without re-raising and aborting the whole composition run.
    """

    def __init__(self, code: str, message: str, *, stage: str, evidence: Mapping[str, Any]) -> None:
        self.code = code
        self.message = message
        self.stage = stage
        self.evidence = dict(evidence)
        super().__init__(message)


# Deterministic accepted_actual_types-per-node_type mapping (Phase 4 Decision 4), matching
# nintent's `operations.hosts._accepted_actual_types` and `loaders._ACTUAL_TYPE_DEFAULTS`.
# A stored list equal to this mapping (order-independent) means "derived"; anything else
# means an explicit override -- no separate provenance field is needed since the rule is
# deterministic (see p4/plan.md Decision 4).
ACCEPTED_ACTUAL_TYPE_DEFAULTS = {
    "device": frozenset({"device"}),
    "virtual_machine": frozenset({"virtual_machine"}),
    "container": frozenset({"container"}),
    "service_host": frozenset({"device", "virtual_machine", "container"}),
}


def accepted_actual_types_source(node_type: str, accepted_actual_types: Iterable[str]) -> str:
    """Return `"derived"` or `"override"` for a stored `accepted_actual_types` list."""

    canonical = ACCEPTED_ACTUAL_TYPE_DEFAULTS.get(node_type)
    if canonical is not None and frozenset(accepted_actual_types) == canonical:
        return "derived"
    return "override"


@dataclass(frozen=True)
class PlacementInput:
    """Desired binding of one service instance to one node."""

    id: str
    instance_name: str
    deployment_profile: str
    config_schema_version: str
    desired_state: str = "active"
    config: Mapping[str, Any] = field(default_factory=dict)
    service_id: str = ""
    service_slug: str = ""
    instance_role: str | None = None
    assignment_source: str = "manual"
    endpoint_id: str | None = None


@dataclass(frozen=True)
class RealizedState:
    """A realized Nautobot object and its allowlisted actual facts."""

    realized_type: str | None
    facts: ActualFacts
    nautobot_device_id: str | None = None


@dataclass(frozen=True)
class NodeInput:
    """Everything the composer needs about one desired node."""

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


@dataclass(frozen=True)
class ResolvedSshTarget:
    """One production-composed node's immutable, single-generation SSH connection identity.

    fix_sshkey3 Step 2: replaces the split
    `resolve_production_routes(SourceSnapshot, ...) + verify_offered_keys(old_snapshot, ...)`
    API. Every field here comes from the exact `NodeInput`/`EffectiveOperationalValues`
    this composition run used to build the node's `ssh_hosts` entry -- never a
    separately re-resolved snapshot -- so a post-regeneration SSH scan can
    never combine a fresh route with a stale port or identity. Only nodes
    actually included in the composed `ssh_hosts` group receive a target
    (see `compose_production_inventory`); a planned service host missing
    from the map must be treated as unreachable, never silently resolved
    another way.
    """

    slug: str
    desired_node_id: str
    alias: str
    route: str
    port: int
    generation_id: str


@dataclass(frozen=True)
class ProductionComposition:
    """The composed inventory document and its companion report."""

    inventory: dict[str, Any]
    report: dict[str, Any]
    ssh_targets: dict[str, ResolvedSshTarget] = field(default_factory=dict)


def is_production_eligible(node: NodeInput) -> bool:
    """Return whether a desired node enters production inventory scope at all."""

    return (
        node.lifecycle in PRODUCTION_ELIGIBLE_LIFECYCLES
        and node.node_type in PRODUCTION_ELIGIBLE_NODE_TYPES
    )


def unapplied_placement_findings(nodes: Iterable[NodeInput]) -> list[dict[str, Any]]:
    """Return one `active_placement_not_applied` report-drift entry per active
    placement recorded on a production-capable node whose lifecycle is not
    (yet) production-eligible (Step 1.3, discussion.md Example 1).

    A pure helper deliberately independent of deployment profiles: it never
    maps or validates placement `config` against a profile, so it stays
    correct even when profiles are unreadable/missing (the drift comparator's
    degraded-profile path still needs this finding). `node_type`-only
    ineligibility (a container, for instance) is out of this code's scope --
    only the lifecycle gate identified by Phase 0 is covered.
    """

    entries: list[dict[str, Any]] = []
    for node in sorted(nodes, key=lambda item: item.slug):
        if node.node_type not in PRODUCTION_ELIGIBLE_NODE_TYPES:
            continue
        if node.lifecycle in PRODUCTION_ELIGIBLE_LIFECYCLES:
            continue
        for placement in sorted(node.placements, key=lambda item: (item.instance_name, item.id)):
            if placement.desired_state != "active":
                continue
            entries.append(
                {
                    "code": ACTIVE_PLACEMENT_NOT_APPLIED,
                    "desired_node_slug": node.slug,
                    "desired_node": node.name,
                    "desired_node_id": node.id,
                    "node_lifecycle": node.lifecycle,
                    "eligible_lifecycles": sorted(PRODUCTION_ELIGIBLE_LIFECYCLES),
                    "placement": _placement_evidence(placement),
                }
            )
    return entries


def compose_production_inventory(
    nodes: Iterable[NodeInput],
    profiles: Mapping[str, Any],
    *,
    generation_id: str,
    generated_at: str,
    deployment_profile_digest: str,
    ssh_known_hosts_file: str | None = None,
) -> ProductionComposition:
    """Compose a deterministic schema 3.0 production inventory plus a schema 3.0 report.

    ``ssh_known_hosts_file`` is the resolved managed known_hosts path
    (``cfg.resolved_ssh_known_hosts_file()``); when given, every eligible
    ``ssh_hosts`` member gets ``nctl_ssh_host_key_alias`` and
    ``ansible_ssh_common_args`` derived from its DesiredNode UUID. The real
    `nctl render production` caller (`production_render.py`) must always
    supply it; the drift comparator's internal composition (which never
    renders to disk) and tests unconcerned with SSH trust vars may omit it.

    Global contract violations raise :class:`ContractError` and abort the whole
    run (the caller preserves the previous inventory).  Host-specific actual
    state problems skip only the affected host with a structured reason.

    The Ansible inventory document is built by the same eligible-node loop this
    function has always used, unchanged, for byte-stability (Phase 4 Decision 3).
    Report building is a separate translation pass over per-node composition
    outcomes (`NodeOutcome`) so the closed `nodes` collection cannot perturb
    inventory bytes for equal inputs.
    """

    validated_profiles = validate_deployment_profiles(dict(profiles))
    profile_group_by_name = {name: profile["group"] for name, profile in validated_profiles.items()}

    all_nodes = sorted(nodes, key=lambda node: node.slug)
    eligible_slugs = {node.slug for node in all_nodes if is_production_eligible(node)}

    ssh_hosts: dict[str, dict[str, Any]] = {}
    selector_members: dict[str, set[str]] = {group: set() for group in _CORE_GROUPS}
    service_members: dict[str, set[str]] = {}
    outcomes: dict[str, "NodeOutcome"] = {}
    active_placements = 0
    inactive_placements = 0
    total_placements = 0

    for node in all_nodes:
        total_placements += len(node.placements)

        if node.slug not in eligible_slugs:
            # Operational mechanism is still worth computing for an out-of-scope
            # node (Principle 3: a value the system would infer must be visible
            # even before the node can act on it) but never enters inventory
            # composition at all.
            effective, finding = try_resolve_operational_values(node, generated_at)
            outcomes[node.id] = NodeOutcome(
                state="out_of_scope", reasons=[], effective=effective, finding=finding, active_placement_ids=[]
            )
            inactive_placements += len(node.placements)
            continue

        effective, finding = try_resolve_operational_values(node, generated_at)
        if finding is not None:
            local_error = LocalCompositionError(
                finding["code"], finding["message"], stage="operational_derivation", evidence=finding["evidence"]
            )
            outcomes[node.id] = NodeOutcome(
                state="skipped",
                reasons=[finding["code"]],
                effective=None,
                finding=finding,
                active_placement_ids=[],
                local_error=local_error,
            )
            inactive_placements += len(node.placements)
            continue

        skip_reasons = _host_actual_skip_reasons(node, effective)
        if skip_reasons:
            outcomes[node.id] = NodeOutcome(
                state="skipped",
                reasons=sorted(set(skip_reasons)),
                effective=effective,
                finding=None,
                active_placement_ids=[],
            )
            # Placements on a skipped host are inactive export members and never
            # create dangling group entries.
            inactive_placements += len(node.placements)
            continue

        try:
            host_vars, host_os, route = _compose_host(node, effective, validated_profiles, ssh_known_hosts_file)
        except LocalCompositionError as local_error:
            outcomes[node.id] = NodeOutcome(
                state="skipped",
                reasons=[local_error.code],
                effective=effective,
                finding=None,
                active_placement_ids=[],
                local_error=local_error,
            )
            inactive_placements += len(node.placements)
            continue

        ssh_hosts[node.slug] = host_vars
        selector_members[_OS_SELECTOR_GROUP[host_os]].add(node.slug)
        if effective.power_control.value != "none":
            selector_members["power_managed"].add(node.slug)

        node_active_ids = host_vars.get("nintent_active_placement_ids", [])
        for placement in node.placements:
            if placement.desired_state == "active" and placement.id in node_active_ids:
                service_members.setdefault(profile_group_by_name[placement.deployment_profile], set()).add(node.slug)
                active_placements += 1
            else:
                inactive_placements += 1

        outcomes[node.id] = NodeOutcome(
            state="included",
            reasons=[],
            effective=effective,
            finding=None,
            active_placement_ids=list(node_active_ids),
            host_os=host_os,
            nautobot_device_id=host_vars.get("nautobot_device_id"),
            resolved_route=route,
            resolved_port=effective.ansible_port.value,
        )

    inventory = _build_inventory_document(
        ssh_hosts=ssh_hosts,
        selector_members=selector_members,
        service_members=service_members,
        generation_id=generation_id,
        generated_at=generated_at,
        deployment_profile_digest=deployment_profile_digest,
    )

    # fix_sshkey3 Step 2: one ResolvedSshTarget per node actually included in
    # ssh_hosts -- never for a skipped/out-of-scope node, and never
    # constructed from anything but this same composition run's route/port.
    ssh_targets: dict[str, ResolvedSshTarget] = {}
    if ssh_known_hosts_file is not None:
        for node in all_nodes:
            if node.slug not in ssh_hosts:
                continue
            outcome = outcomes[node.id]
            if outcome.resolved_route is None:
                continue
            ssh_targets[node.slug] = ResolvedSshTarget(
                slug=node.slug,
                desired_node_id=node.id,
                alias=derive_host_key_alias(node.id),
                route=outcome.resolved_route,
                port=outcome.resolved_port if outcome.resolved_port is not None else 22,
                generation_id=generation_id,
            )

    report_nodes = [build_node_report_record(node, outcomes[node.id]) for node in all_nodes]
    applied_placements = sum(
        1
        for record in report_nodes
        for effect in record["actual"]["production"]["placement_effects"]
        if effect["effect"] == "applied"
    )
    not_applied_placements = sum(
        1
        for record in report_nodes
        for effect in record["actual"]["production"]["placement_effects"]
        if effect["effect"] == "not_applied"
    )
    report = {
        "schema_version": PRODUCTION_REPORT_SCHEMA_VERSION,
        "generation_id": generation_id,
        "generated_at": generated_at,
        "report_path": f"production.reports/{generation_id}.json",
        "deployment_profile_digest": deployment_profile_digest,
        "summary": {
            "eligible": len(eligible_slugs),
            "included": len(ssh_hosts),
            "skipped": sum(1 for outcome in outcomes.values() if outcome.state == "skipped"),
            "out_of_scope": sum(1 for outcome in outcomes.values() if outcome.state == "out_of_scope"),
            "placements": total_placements,
            "active_placements": active_placements,
            "inactive_placements": inactive_placements,
            "applied_placements": applied_placements,
            "not_applied_placements": not_applied_placements,
        },
        "nodes": sorted(report_nodes, key=lambda item: item["desired"]["node"]["slug"]),
    }

    # Fail closed: the composer must only ever emit conforming documents.
    validate_production_inventory_document(inventory, validated_profiles)
    validate_production_report_v3(report)
    return ProductionComposition(inventory=inventory, report=report, ssh_targets=ssh_targets)


@dataclass
class NodeOutcome:
    """Per-node composition outcome, kept separate from the inventory-building loop's
    own local variables so the report translation pass (`build_node_report_record`) cannot
    influence inventory bytes.
    """

    state: str  # included | skipped | out_of_scope
    reasons: list[str]
    effective: EffectiveOperationalValues | None
    finding: dict[str, Any] | None
    active_placement_ids: list[str]
    host_os: str | None = None
    nautobot_device_id: str | None = None
    local_error: LocalCompositionError | None = None
    resolved_route: str | None = None
    resolved_port: int | None = None


def resolve_effective_route(node: NodeInput, effective: EffectiveOperationalValues) -> dict[str, Any]:
    """Resolve the connection variables (including `ansible_host`) production would use for `node`.

    Extracted so `nctl_core.reconcile.ssh_preflight` can ask "what route would
    production actually connect over right now" without duplicating this
    resolution logic (fix_sshkey Step 5) -- the composed inventory itself
    never exports `ansible_host` per host (see `_compose_host` below), so this
    is the one place both call sites can get it from.
    """
    declared = effective.actual_state_policy.value == "declared"
    realized = None if declared else node.realized
    facts: ActualFacts | None = realized.facts if realized else None
    selected = effective.selected_endpoint
    return resolve_connection_variables(
        inventory_hostname=node.slug,
        actual_state_policy=effective.actual_state_policy.value,
        connection_path=effective.connection_path.value,
        actual_local_ip=facts.local_ip if facts else None,
        local_endpoint=selected.evidence() if effective.connection_path.value == "local" else None,
        tailscale_endpoint=selected.evidence() if effective.connection_path.value == "tailscale" else None,
    )


def try_resolve_operational_values(
    node: NodeInput, generated_at: str
) -> tuple[EffectiveOperationalValues | None, dict[str, Any] | None]:
    try:
        return (
            resolve_operational_values(
                node_id=node.id,
                node_slug=node.slug,
                endpoints=node.endpoints,
                override=node.operational_override,
                realized_type=node.realized.realized_type if node.realized else None,
                facts=node.realized.facts if node.realized else None,
                generated_at=generated_at,
            ),
            None,
        )
    except DerivationFailure as exc:
        return None, {
            "code": exc.code,
            "field": exc.field,
            "message": exc.message,
            "evidence": {"field": exc.field, **dict(exc.evidence)},
        }


def build_node_report_record(node: NodeInput, outcome: "NodeOutcome") -> dict[str, Any]:
    """Build one closed report-3.0 node record (Phase 4 Decision 2) from a node and its
    already-computed composition `NodeOutcome`. Pure translation -- performs no derivation
    and touches no inventory-building state.
    """

    placement_effects = [
        _placement_effect_entry(placement, outcome) for placement in sorted(node.placements, key=lambda p: p.instance_name)
    ]
    return {
        "desired": {
            "node": {
                "id": node.id,
                "slug": node.slug,
                "name": node.name,
                "lifecycle": node.lifecycle,
                "node_type": node.node_type,
                "role": node.role,
                "accepted_actual_types": sorted(node.accepted_actual_types),
                "accepted_actual_types_source": accepted_actual_types_source(node.node_type, node.accepted_actual_types),
            },
            "endpoints": [
                {
                    "id": endpoint.id,
                    "name": endpoint.name,
                    "endpoint_type": endpoint.endpoint_type,
                    "ip_address": endpoint.ip_address,
                    "dns_name": endpoint.dns_name,
                    "mdns_name": endpoint.mdns_name,
                }
                for endpoint in sorted(node.endpoints, key=lambda item: item.id)
            ],
            "placements": [_placement_desired_entry(placement) for placement in sorted(node.placements, key=lambda p: p.instance_name)],
            "operational_override": _operational_override_entry(node.operational_override),
        },
        "actual": {
            "operational_values": outcome.effective.as_dict() if outcome.effective is not None else {},
            "operational_finding": outcome.finding,
            "local_findings": (
                [
                    {
                        "code": outcome.local_error.code,
                        "severity": "error",
                        "message": outcome.local_error.message,
                        "stage": outcome.local_error.stage,
                        "evidence": outcome.local_error.evidence,
                    }
                ]
                if outcome.local_error is not None
                else []
            ),
            "production": {
                "state": outcome.state,
                "reasons": outcome.reasons,
                "placement_effects": placement_effects,
            },
        },
    }


def _placement_desired_entry(placement: PlacementInput) -> dict[str, Any]:
    return {
        "id": placement.id,
        "service_id": placement.service_id,
        "service_slug": placement.service_slug,
        "instance_name": placement.instance_name,
        "desired_state": placement.desired_state,
        "instance_role": placement.instance_role,
        "deployment_profile": placement.deployment_profile,
        "config_schema_version": placement.config_schema_version,
        "config": dict(placement.config),
        "assignment_source": placement.assignment_source,
        "endpoint_id": placement.endpoint_id,
    }


def _operational_override_entry(override: OperationalOverride | None) -> dict[str, Any] | None:
    if override is None:
        return None
    return {
        "id": override.id,
        "declared_host_os": override.declared_host_os,
        "connection_path": override.connection_path,
        "ansible_port": override.ansible_port,
        "power_control": override.power_control,
        "is_laptop": override.is_laptop,
        "local_endpoint_id": override.local_endpoint_id,
        "tailscale_endpoint_id": override.tailscale_endpoint_id,
    }


def _placement_effect_entry(placement: PlacementInput, outcome: "NodeOutcome") -> dict[str, Any]:
    if outcome.state == "included":
        if placement.id in outcome.active_placement_ids:
            effect, reason = "applied", None
        else:
            effect, reason = "inactive_by_intent", None
    elif placement.desired_state != "active":
        effect, reason = "inactive_by_intent", None
    else:
        if outcome.reasons:
            reason = outcome.reasons[0]
        elif outcome.state == "out_of_scope":
            reason = "node_out_of_scope"
        elif outcome.state == "unknown":
            reason = "production_unknown"
        else:
            reason = "node_skipped"
        effect = "not_applied"
    return {
        "placement_id": placement.id,
        "instance_name": placement.instance_name,
        "effect": effect,
        "reason": reason,
    }


def _host_actual_skip_reasons(
    node: NodeInput,
    effective: EffectiveOperationalValues,
) -> list[str]:
    """Return consumer-specific fact gaps left after successful derivation."""

    if effective.actual_state_policy.value != "required":
        return []
    facts = node.realized.facts if node.realized else None
    if effective.power_control.value == "wol" and (facts is None or not facts.mac_address):
        return ["missing_mac_address"]
    return []


def _compose_host(
    node: NodeInput,
    effective: EffectiveOperationalValues,
    profiles: Mapping[str, Any],
    ssh_known_hosts_file: str | None,
) -> tuple[dict[str, Any], str, str | None]:
    """Build the ssh_hosts host variables for one included node.

    Returns `(host_vars, host_os, route)`: `route` is the exact
    `ansible_host` this composition resolved for the node before it was
    popped from the exported vars (fix_sshkey3 Step 2's `ResolvedSshTarget`
    source of truth) -- `None` only in the pathological case where
    `resolve_effective_route` returned no `ansible_host` at all.
    """

    declared = effective.actual_state_policy.value == "declared"
    realized = None if declared else node.realized
    facts: ActualFacts | None = realized.facts if realized else None

    try:
        connection = resolve_effective_route(node, effective)
    except ContractError as exc:
        _localize(
            exc,
            NODE_LOCAL_CODES,
            stage="connection",
            evidence={"operational_values": effective.as_dict()},
        )
    # ansible_host is resolved in generated group_vars/all, not exported per host.
    route = connection.pop("ansible_host", None)

    base_vars: dict[str, Any] = {
        "host_os": effective.host_os.value,
        "power_control": effective.power_control.value,
        "is_laptop": effective.is_laptop.value,
        "nintent_desired_node_id": node.id,
    }
    if ssh_known_hosts_file is not None:
        ssh_alias = derive_host_key_alias(node.id)
        base_vars["nctl_ssh_host_key_alias"] = ssh_alias
        base_vars["ansible_ssh_common_args"] = build_ansible_ssh_common_args(ssh_alias, ssh_known_hosts_file)
    base_vars.update(connection)
    if effective.ansible_port.value is not None:
        base_vars["ansible_port"] = effective.ansible_port.value
    if facts and facts.mac_address:
        base_vars["mac_address"] = facts.mac_address
    if facts and facts.network_interface:
        base_vars["network_interface"] = facts.network_interface
    if realized and realized.nautobot_device_id:
        base_vars["nautobot_device_id"] = realized.nautobot_device_id

    active_ids: list[str] = []
    assignments: list[tuple[str, Mapping[str, Any]]] = [(f"node:{node.slug}", base_vars)]
    for placement in sorted(node.placements, key=lambda item: item.instance_name):
        if placement.desired_state != "active":
            continue
        try:
            mapped = map_placement_config(
                placement.deployment_profile,
                placement.config_schema_version,
                dict(placement.config),
                profiles,
            )
        except ContractError as exc:
            _localize(
                exc,
                PLACEMENT_LOCAL_CODES,
                stage="placement_config",
                evidence={"placement": _placement_evidence(placement)},
            )
        assignments.append((f"placement:{placement.instance_name}", mapped))
        active_ids.append(placement.id)

    try:
        host_vars = merge_host_variables(assignments)
    except ContractError as exc:
        _localize(
            exc,
            MERGE_LOCAL_CODES,
            stage="host_merge",
            evidence={
                "assignments": sorted(source for source, _variables in assignments),
                "active_placement_ids": sorted(active_ids),
            },
        )
    host_vars["nintent_active_placement_ids"] = sorted(active_ids)

    return host_vars, effective.host_os.value, route


def _localize(exc: ContractError, allowlist: frozenset[str], *, stage: str, evidence: Mapping[str, Any]) -> None:
    """Re-raise `exc` as a `LocalCompositionError` if its code is in this
    stage's allowlist; otherwise re-raise it unchanged so an unexpected code
    still aborts the whole run (Decision 2's "never silently downgraded").
    """

    if exc.code in allowlist:
        raise LocalCompositionError(exc.code, str(exc), stage=stage, evidence=evidence) from exc
    raise exc


def _placement_evidence(placement: PlacementInput) -> dict[str, Any]:
    return {
        "id": placement.id,
        "instance_name": placement.instance_name,
        "deployment_profile": placement.deployment_profile,
        "config_schema_version": placement.config_schema_version,
        "desired_state": placement.desired_state,
        "config": dict(placement.config),
    }


def _build_inventory_document(
    *,
    ssh_hosts: Mapping[str, dict[str, Any]],
    selector_members: Mapping[str, set[str]],
    service_members: Mapping[str, set[str]],
    generation_id: str,
    generated_at: str,
    deployment_profile_digest: str,
) -> dict[str, Any]:
    children: dict[str, Any] = {
        "ssh_hosts": {"hosts": {hostname: ssh_hosts[hostname] for hostname in sorted(ssh_hosts)}}
    }
    for group in ("linux", "macos", "haos", "power_managed"):
        children[group] = {"hosts": {hostname: {} for hostname in sorted(selector_members[group])}}
    for group in sorted(service_members):
        children[group] = {"hosts": {hostname: {} for hostname in sorted(service_members[group])}}
    return {
        "all": {
            "vars": {
                "nintent_inventory_schema_version": PRODUCTION_INVENTORY_SCHEMA_VERSION,
                "nintent_generation_id": generation_id,
                "nintent_generated_at": generated_at,
                "nintent_report_path": f"production.reports/{generation_id}.json",
                "nintent_deployment_profile_digest": deployment_profile_digest,
            },
            "children": children,
        }
    }


def render_production_inventory_yml(composition: ProductionComposition) -> str:
    """Return a deterministic, schema-versioned production inventory YAML."""

    header = [
        "# Generated by nctl render production",
        f"# schema_version: {PRODUCTION_INVENTORY_SCHEMA_VERSION}",
        f"# generation_id: {composition.report['generation_id']}",
    ]
    body = yaml.safe_dump(composition.inventory, sort_keys=True, default_flow_style=False).rstrip()
    return "\n".join(header) + "\n" + body + "\n"


def render_production_report_json(composition: ProductionComposition) -> str:
    """Return a deterministic JSON companion report."""

    return json.dumps(composition.report, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
