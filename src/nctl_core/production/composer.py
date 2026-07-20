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

from nctl_core.sources.actual import ActualFacts, actual_type_problem, missing_required_facts

from .contract import (
    PRODUCTION_INVENTORY_SCHEMA_VERSION,
    ContractError,
    actual_state_problem,
    evaluate_platform_policy,
    map_placement_config,
    merge_host_variables,
    resolve_connection_variables,
    validate_deployment_profiles,
    validate_endpoint_ownership,
    validate_production_inventory_document,
    validate_production_report,
)
from .derivation import EndpointCandidate, OperationalOverride

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
        "missing_operational_config",
        "invalid_actual_state_policy",
        "unsupported_observed_host_os",
        "invalid_platform_power",
        "endpoint_node_mismatch",
        "unresolved_connection_path",
        "invalid_connection_path",
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

# The complete Phase 1 node-targeted code vocabulary (16 codes): every code
# `reconcile/classify.py` must register as MANUAL_REVIEW with no reconciler,
# and every code the planner must treat as a production-actuation blocker
# for its owning node.
PHASE1_LOCAL_CODES = LOCAL_COMPOSITION_CODES | OPERATIONAL_DERIVATION_CODES | {
    ACTIVE_PLACEMENT_NOT_APPLIED
}


class LocalCompositionError(Exception):
    """One target-local Group C failure, carrying enough context to become a
    structured `report["errors"]` entry and `report["skipped"]` reason
    without re-raising and aborting the whole composition run.
    """

    def __init__(self, code: str, message: str, *, stage: str, evidence: Mapping[str, Any]) -> None:
        self.code = code
        self.message = message
        self.stage = stage
        self.evidence = dict(evidence)
        super().__init__(message)


@dataclass(frozen=True)
class EndpointInput:
    """A node-scoped desired endpoint selected by a placement or operational config."""

    name: str
    endpoint_type: str
    node_slug: str
    ip_address: str | None = None
    dns_name: str | None = None
    mdns_name: str | None = None

    def as_connection_mapping(self) -> dict[str, Any]:
        return {
            "ip_address": self.ip_address,
            "dns_name": self.dns_name,
            "mdns_name": self.mdns_name,
        }


@dataclass(frozen=True)
class OperationalConfigInput:
    """Typed non-service execution policy for one desired node."""

    id: str
    actual_state_policy: str
    connection_path: str
    power_control: str = "none"
    is_laptop: bool = False
    expected_host_os: str | None = None
    declared_host_os: str | None = None
    local_endpoint: EndpointInput | None = None
    tailscale_endpoint: EndpointInput | None = None
    ansible_port: int | None = None


@dataclass(frozen=True)
class PlacementInput:
    """Desired binding of one service instance to one node."""

    id: str
    instance_name: str
    deployment_profile: str
    config_schema_version: str
    desired_state: str = "active"
    config: Mapping[str, Any] = field(default_factory=dict)


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
    endpoints: tuple[EndpointCandidate, ...] = ()
    operational_override: OperationalOverride | None = None
    operational_config: OperationalConfigInput | None = None
    placements: tuple[PlacementInput, ...] = ()
    realized: RealizedState | None = None


@dataclass(frozen=True)
class ProductionComposition:
    """The composed inventory document and its companion report."""

    inventory: dict[str, Any]
    report: dict[str, Any]


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
) -> ProductionComposition:
    """Compose a deterministic schema 1.0 production inventory and report.

    Global contract violations raise :class:`ContractError` and abort the whole
    run (the caller preserves the previous inventory).  Host-specific actual
    state problems skip only the affected host with a structured reason.
    """

    validated_profiles = validate_deployment_profiles(dict(profiles))
    profile_group_by_name = {name: profile["group"] for name, profile in validated_profiles.items()}

    all_nodes = list(nodes)
    eligible = sorted(
        (node for node in all_nodes if is_production_eligible(node)),
        key=lambda node: node.slug,
    )

    ssh_hosts: dict[str, dict[str, Any]] = {}
    selector_members: dict[str, set[str]] = {group: set() for group in _CORE_GROUPS}
    service_members: dict[str, set[str]] = {}
    report_hosts: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    drift: list[dict[str, Any]] = []
    active_placements = 0
    inactive_placements = 0
    total_placements = 0

    for node in eligible:
        total_placements += len(node.placements)
        operational_config = node.operational_config
        if operational_config is None:
            # A missing operational config is owned by this one node: skip
            # only this host, matching every other Group C failure below.
            local_error = LocalCompositionError(
                "missing_operational_config",
                f"production-eligible node {node.slug!r} has no operational config",
                stage="operational_config",
                evidence={"node": {"slug": node.slug, "id": node.id, "lifecycle": node.lifecycle}},
            )
            skipped.append(_local_skip_entry(node, local_error))
            errors.append(_local_error_entry(node, local_error))
            inactive_placements += len(node.placements)
            continue

        skip_reasons = _host_actual_skip_reasons(node, operational_config, generated_at)
        if skip_reasons:
            skipped.append(
                {
                    "item_type": "desired_node",
                    "desired_node": node.name,
                    "desired_node_slug": node.slug,
                    "desired_node_id": node.id,
                    "reasons": sorted(set(skip_reasons)),
                }
            )
            # Placements on a skipped host are inactive export members and never
            # create dangling group entries.
            inactive_placements += len(node.placements)
            continue

        try:
            host_vars, host_os, node_drift = _compose_host(node, operational_config, validated_profiles)
        except LocalCompositionError as local_error:
            skipped.append(_local_skip_entry(node, local_error))
            errors.append(_local_error_entry(node, local_error))
            inactive_placements += len(node.placements)
            continue
        ssh_hosts[node.slug] = host_vars
        selector_members[_OS_SELECTOR_GROUP[host_os]].add(node.slug)
        if operational_config.power_control != "none":
            selector_members["power_managed"].add(node.slug)
        drift.extend(node_drift)

        node_active_ids = host_vars.get("nintent_active_placement_ids", [])
        for placement in node.placements:
            if placement.desired_state == "active" and placement.id in node_active_ids:
                service_members.setdefault(profile_group_by_name[placement.deployment_profile], set()).add(node.slug)
                active_placements += 1
            else:
                inactive_placements += 1

        report_hosts.append(
            {
                "inventory_hostname": node.slug,
                "desired_node_id": node.id,
                "host_os": host_os,
                "connection_path": operational_config.connection_path,
                "actual_state_policy": operational_config.actual_state_policy,
                "nautobot_device_id": host_vars.get("nautobot_device_id"),
                "active_placement_ids": list(node_active_ids),
            }
        )

    drift.extend(unapplied_placement_findings(all_nodes))

    inventory = _build_inventory_document(
        ssh_hosts=ssh_hosts,
        selector_members=selector_members,
        service_members=service_members,
        generation_id=generation_id,
        generated_at=generated_at,
        deployment_profile_digest=deployment_profile_digest,
    )
    report = {
        "schema_version": PRODUCTION_INVENTORY_SCHEMA_VERSION,
        "generation_id": generation_id,
        "generated_at": generated_at,
        "report_path": f"production.reports/{generation_id}.json",
        "deployment_profile_digest": deployment_profile_digest,
        "summary": {
            "eligible": len(eligible),
            "included": len(ssh_hosts),
            "skipped": len(skipped),
            "placements": total_placements,
            "active_placements": active_placements,
            "inactive_placements": inactive_placements,
        },
        "hosts": sorted(report_hosts, key=lambda item: item["inventory_hostname"]),
        "skipped": sorted(skipped, key=lambda item: item["desired_node_slug"]),
        "drift": sorted(drift, key=lambda item: (item["desired_node_slug"], item["code"])),
        "errors": sorted(errors, key=_error_sort_key),
    }

    # Fail closed: the composer must only ever emit conforming documents.
    validate_production_inventory_document(inventory, validated_profiles)
    validate_production_report(report)
    return ProductionComposition(inventory=inventory, report=report)


def _host_actual_skip_reasons(
    node: NodeInput,
    operational_config: OperationalConfigInput,
    generated_at: str,
) -> list[str]:
    """Return host-skip reasons for a node that cannot be actual-backed.

    Declared nodes (such as HAOS) never require a realized object or nodeutils
    data, so they are never skipped here.
    """

    if operational_config.actual_state_policy != "required":
        return []

    realized = node.realized
    realized_type = realized.realized_type if realized else None
    type_problem = actual_type_problem(realized_type)
    if type_problem:
        return [type_problem]

    facts = realized.facts
    reasons: list[str] = []
    freshness_problem = actual_state_problem(facts.collected_at, generated_at)
    if freshness_problem:
        reasons.append(freshness_problem)
    consumers = {"host_os"}
    if operational_config.power_control == "wol":
        consumers.add("wol")
    reasons.extend(missing_required_facts(facts, consumers))
    return reasons


def _compose_host(
    node: NodeInput,
    operational_config: OperationalConfigInput,
    profiles: Mapping[str, Any],
) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
    """Build the ssh_hosts host variables for one included node."""

    declared = operational_config.actual_state_policy == "declared"
    realized = None if declared else node.realized
    facts: ActualFacts | None = realized.facts if realized else None
    observed_system = facts.observed_system if facts else None

    # One tested place normalizes the observed system into host_os and validates
    # the platform/power combination; a node-owned unsafe combination is local.
    try:
        host_os, policy_drift = evaluate_platform_policy(
            actual_state_policy=operational_config.actual_state_policy,
            power_control=operational_config.power_control,
            expected_host_os=operational_config.expected_host_os,
            declared_host_os=operational_config.declared_host_os,
            observed_system=observed_system,
        )
    except ContractError as exc:
        _localize(
            exc,
            NODE_LOCAL_CODES,
            stage="platform_policy",
            evidence={
                "operational_config": {
                    "actual_state_policy": operational_config.actual_state_policy,
                    "power_control": operational_config.power_control,
                    "expected_host_os": operational_config.expected_host_os,
                    "declared_host_os": operational_config.declared_host_os,
                },
                "observed_system": observed_system,
            },
        )

    try:
        local_endpoint = _validated_endpoint(node, operational_config.local_endpoint)
        tailscale_endpoint = _validated_endpoint(node, operational_config.tailscale_endpoint)
    except ContractError as exc:
        _localize(
            exc,
            NODE_LOCAL_CODES,
            stage="endpoint_ownership",
            evidence={"node": {"slug": node.slug, "id": node.id}},
        )

    try:
        connection = resolve_connection_variables(
            inventory_hostname=node.slug,
            actual_state_policy=operational_config.actual_state_policy,
            connection_path=operational_config.connection_path,
            actual_local_ip=facts.local_ip if facts else None,
            local_endpoint=local_endpoint.as_connection_mapping() if local_endpoint else None,
            tailscale_endpoint=tailscale_endpoint.as_connection_mapping() if tailscale_endpoint else None,
        )
    except ContractError as exc:
        _localize(
            exc,
            NODE_LOCAL_CODES,
            stage="connection",
            evidence={"operational_config": {"connection_path": operational_config.connection_path}},
        )
    # ansible_host is resolved in generated group_vars/all, not exported per host.
    connection.pop("ansible_host", None)

    base_vars: dict[str, Any] = {
        "host_os": host_os,
        "power_control": operational_config.power_control,
        "is_laptop": operational_config.is_laptop,
        "nintent_desired_node_id": node.id,
        "nintent_operational_config_id": operational_config.id,
    }
    base_vars.update(connection)
    if operational_config.ansible_port is not None:
        base_vars["ansible_port"] = operational_config.ansible_port
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

    drift = [dict(entry, desired_node_slug=node.slug) for entry in policy_drift]
    return host_vars, host_os, drift


def _validated_endpoint(node: NodeInput, endpoint: EndpointInput | None) -> EndpointInput | None:
    if endpoint is None:
        return None
    validate_endpoint_ownership(node.slug, endpoint.node_slug)
    return endpoint


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


def _local_skip_entry(node: NodeInput, local_error: LocalCompositionError) -> dict[str, Any]:
    return {
        "item_type": "desired_node",
        "desired_node": node.name,
        "desired_node_slug": node.slug,
        "desired_node_id": node.id,
        "reasons": [local_error.code],
    }


def _local_error_entry(node: NodeInput, local_error: LocalCompositionError) -> dict[str, Any]:
    return {
        "item_type": "desired_node",
        "desired_node": node.name,
        "desired_node_slug": node.slug,
        "desired_node_id": node.id,
        "code": local_error.code,
        "severity": "error",
        "message": local_error.message,
        "stage": local_error.stage,
        "evidence": local_error.evidence,
    }


def _error_sort_key(entry: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
    placement = entry.get("evidence", {}).get("placement", {})
    return (
        entry["desired_node_slug"],
        entry["code"],
        entry["stage"],
        placement.get("instance_name") or "",
        placement.get("id") or "",
    )


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
