"""Ported deterministic desired-vs-actual evaluation logic (Phase 2 Step 4).

Port of nintent's `nautobot_intent_catalog/evaluations.py` (1266 lines), the
pure computation behind the `Evaluate Node/Endpoint/Service Intent` Jobs.
Behavior (candidate scoring weights, gap codes, DHCP-readiness rule,
IP-range classification/overlap detection) is unchanged; only input access
changed from `getattr`-on-ORM to the Step 1 pydantic read-models
(`sources.desired`/`sources.actual`), which are already typed, so the
original's duck-typed `getattr(obj, "field", None)` chains are replaced with
plain attribute access.

Structural deviations from the ORM version, all a consequence of "typed
read-models" not being the same shape as live Django relations:

- The original walked `desired_node.realized_device`/`.realized_vm` directly
  (a Django FK dereference). Here the caller resolves `realized_device_id`/
  `realized_vm_id` against the `ActualSnapshot`'s device/VM lists and passes
  the results in; a dangling id (references an object that no longer exists)
  is *not* re-reported here — that is the Step 3 `node_existence`
  comparator's `realized_device_missing`/`realized_vm_missing` job, so
  `evaluate_node_intent` simply treats a dangling id as "not realized" and
  falls through to candidate ranking, rather than duplicating the diagnostic.
- `ActualVirtualMachine` (Step 1) carries only `id`/`name` — no custom fields,
  no interfaces — because the Step 1 GraphQL query never fetched them (no
  current consumer needed VM facts). VM candidates therefore only ever score
  on name/hostname (`name_or_hostname`, weight 50); the serial/uuid/platform
  weights (80/80/10) are structurally unreachable for VMs until Step 1's
  actual query grows VM facts. Device candidates are unaffected and score on
  the full original rubric.
- `EvaluationPayload.source_hash`/`.as_defaults()` are dropped: they existed
  to key/deduplicate persisted `IntentEvaluation` rows (Decision 1 in
  `p2/plan.md` deletes that persistence — evaluations are computed fresh on
  every read), so there is nothing to hash or default-fill here.
- `evaluate_endpoint_intent` takes `desired_node` and `realized_ip` as
  explicit parameters instead of walking `desired_endpoint.desired_node`/
  `.realized_ip_address` (the read-model only carries `node_id` and
  `realized_ip_address_id`); the caller (a comparator or the dnsmasq fetch
  path) resolves both from the `SourceSnapshot` once and passes them in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from ipaddress import ip_address as _parse_ip_address, ip_interface
import re
from typing import Any, Iterable, Mapping

from nctl_core.names import canonical_node_name
from nctl_core.sources.actual import ActualDevice, ActualInterface, ActualIPAddress, ActualVirtualMachine
from nctl_core.sources.desired import DesiredDependency, DesiredEndpoint, DesiredIPRange, DesiredNode, DesiredService

NODE_TARGET_TYPE = "desired_node"
ENDPOINT_TARGET_TYPE = "desired_endpoint"
SERVICE_TARGET_TYPE = "desired_service"

# Gap codes whose nintent severity was "missing"/"unknown" because there is no
# reliable actual/observed data to compare against at all (as opposed to a
# real disagreement) -- Step 3's `drift.status.UNKNOWN_CODES` extends this
# set so `nctl drift` resolves the same target to `unknown`, not `drifting`.
NO_DATA_GAP_CODES = frozenset(
    {
        "missing_actual_node",
        "missing_service_lifecycle",
        "service_observation_missing",
        "service_observation_stale",
    }
)


@dataclass(frozen=True)
class NormalizedIPRange:
    source: DesiredIPRange
    facts: dict[str, Any]
    start_ip: Any
    end_ip: Any
    sort_key: tuple[int, int, int, str, str, str]


@dataclass(frozen=True)
class EvaluationResult:
    """The computed-fresh equivalent of a persisted `IntentEvaluation` row."""

    target_type: str
    target_id: str
    status: str
    deterministic_summary: dict[str, Any]
    actual_refs: list[dict[str, Any]]
    observed_facts: dict[str, Any]
    expected_facts: dict[str, Any]
    gap_summary: dict[str, Any]
    recommended_actions: list[dict[str, Any]] = field(default_factory=list)

    def as_row(self) -> dict[str, Any]:
        """Shape matching the `IntentEvaluation` GraphQL row nctl_core.dnsmasq expects."""
        return {
            "target_id": self.target_id,
            "observed_facts": self.observed_facts,
            "deterministic_summary": self.deterministic_summary,
            "actual_refs": self.actual_refs,
        }


def normalize_endpoint_ip_string(value: Any) -> str:
    """Return a host IP string for endpoint intent, or an empty string when invalid."""
    normalized = _strict_host_address(value)
    return str(normalized) if normalized is not None else ""


def normalize_desired_range_addresses(ip_range: DesiredIPRange) -> dict[str, Any]:
    """Return normalized range address data and deterministic validation errors."""
    start_text = _text(ip_range.start_address)
    end_text = _text(ip_range.end_address)
    start_ip = _strict_host_address(start_text)
    end_ip = _strict_host_address(end_text)
    errors: list[str] = []

    if not start_text:
        errors.append("missing_start_address")
    elif start_ip is None:
        errors.append("invalid_start_address")

    if not end_text:
        errors.append("missing_end_address")
    elif end_ip is None:
        errors.append("invalid_end_address")

    if start_ip is not None and end_ip is not None:
        if start_ip.version != end_ip.version:
            errors.append("address_family_mismatch")
        elif int(start_ip) > int(end_ip):
            errors.append("range_start_after_end")

    return {
        "start_address": str(start_ip) if start_ip is not None else start_text,
        "end_address": str(end_ip) if end_ip is not None else end_text,
        "valid": not errors,
        "errors": errors,
    }


def desired_ip_range_facts(ip_range: DesiredIPRange) -> dict[str, Any]:
    """Return serializable facts for a `DesiredIPRange`."""
    normalized = normalize_desired_range_addresses(ip_range)
    facts = {
        "desired_ip_range_id": ip_range.id,
        "name": _text(ip_range.name),
        "slug": _text(ip_range.slug),
        "start_address": normalized["start_address"],
        "end_address": normalized["end_address"],
        "range_policy": _text(ip_range.range_policy),
        "lifecycle": _text(ip_range.lifecycle),
        "generate_dnsmasq": bool(ip_range.generate_dnsmasq),
    }
    if not normalized["valid"]:
        facts["valid"] = False
        facts["errors"] = normalized["errors"]
    return facts


def matching_desired_ip_ranges(endpoint_ip: Any, range_candidates: Iterable[DesiredIPRange]) -> list[dict[str, Any]]:
    return classify_endpoint_ip_ranges(endpoint_ip, range_candidates)["matching_ranges"]


def invalid_desired_ip_ranges(range_candidates: Iterable[DesiredIPRange]) -> list[dict[str, Any]]:
    classified = _classified_ip_ranges(range_candidates)
    return [entry["facts"] for entry in classified["invalid"]]


def overlapping_desired_ip_ranges(range_candidates: Iterable[DesiredIPRange]) -> list[dict[str, Any]]:
    valid_ranges = _classified_ip_ranges(range_candidates)["valid"]
    return _overlap_records(valid_ranges)


def classify_endpoint_ip_ranges(endpoint_ip: Any, range_candidates: Iterable[DesiredIPRange]) -> dict[str, Any]:
    """Classify an endpoint IP against desired ranges."""
    normalized_endpoint_ip = _strict_host_address(endpoint_ip)
    classified = _classified_ip_ranges(range_candidates)
    matching_ranges: list[NormalizedIPRange] = []

    if normalized_endpoint_ip is not None:
        for ip_range in classified["valid"]:
            if (
                normalized_endpoint_ip.version == ip_range.start_ip.version
                and int(ip_range.start_ip) <= int(normalized_endpoint_ip) <= int(ip_range.end_ip)
            ):
                matching_ranges.append(ip_range)

    overlap_records = _overlap_records(classified["valid"])
    matching_ids = {entry.facts["desired_ip_range_id"] for entry in matching_ranges}
    matching_keys = {_range_identity_key(entry.facts) for entry in matching_ranges}
    overlapping_matching_ranges = [
        overlap
        for overlap in overlap_records
        if (
            overlap["first"].get("desired_ip_range_id") in matching_ids
            or overlap["second"].get("desired_ip_range_id") in matching_ids
            or _range_identity_key(overlap["first"]) in matching_keys
            or _range_identity_key(overlap["second"]) in matching_keys
        )
    ]

    return {
        "endpoint_ip": str(normalized_endpoint_ip) if normalized_endpoint_ip is not None else _text(endpoint_ip),
        "endpoint_ip_valid": normalized_endpoint_ip is not None or not _text(endpoint_ip),
        "matching_ranges": [entry.facts for entry in sorted(matching_ranges, key=lambda entry: entry.sort_key)],
        "invalid_ranges": [entry["facts"] for entry in classified["invalid"]],
        "overlapping_ranges": overlap_records,
        "overlapping_matching_ranges": overlapping_matching_ranges,
    }


def evaluate_node_intent(
    desired_node: DesiredNode,
    *,
    device_candidates: Iterable[ActualDevice] = (),
    vm_candidates: Iterable[ActualVirtualMachine] = (),
    interfaces_by_device_id: Mapping[str, list[ActualInterface]] | None = None,
    realized_device: ActualDevice | None = None,
    realized_vm: ActualVirtualMachine | None = None,
) -> EvaluationResult:
    """Compare a `DesiredNode` with actual Device/VM candidates."""
    interfaces_by_device_id = interfaces_by_device_id or {}
    expected = _expected_node_facts(desired_node)
    accepted_actual_types = set(expected["accepted_actual_types"])
    realized = _realized_node_objects(desired_node, realized_device, realized_vm)
    actual_refs: list[dict[str, Any]] = []
    observed: dict[str, Any] = {"candidates": []}
    gaps: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []

    if len(realized) > 1:
        actual_refs = [_actual_ref(object_type, obj) for object_type, obj in realized]
        observed["actual"] = [_actual_node_facts(object_type, obj, interfaces_by_device_id) for object_type, obj in realized]
        gaps.append({"code": "multiple_realized_links", "severity": "conflict"})
        status = "conflict"
    elif len(realized) == 1:
        object_type, actual = realized[0]
        actual_refs = [_actual_ref(object_type, actual)]
        actual_facts = _actual_node_facts(object_type, actual, interfaces_by_device_id)
        observed["actual"] = actual_facts
        actual_type = _actual_type_for_object_type(object_type)
        if actual_type not in accepted_actual_types:
            gaps.append(
                {
                    "code": "realized_actual_type_not_accepted",
                    "severity": "conflict",
                    "expected": expected["accepted_actual_types"],
                    "actual": actual_type,
                }
            )
        else:
            gaps.extend(_node_mismatches(expected, actual_facts))
        status = "conflict" if gaps else "satisfied"
    else:
        candidates = _rank_node_candidates(
            expected,
            device_candidates=device_candidates,
            vm_candidates=vm_candidates,
            interfaces_by_device_id=interfaces_by_device_id,
        )
        observed["candidates"] = [candidate for candidate in candidates if candidate["score"] > 0]
        strong = [candidate for candidate in observed["candidates"] if candidate["score"] >= 40]
        if not strong:
            gaps.append({"code": "missing_actual_node", "severity": "missing"})
            actions.append(
                {
                    "action": "link_desired_node_to_actual",
                    "target": _target_ref(desired_node.id, desired_node.name),
                    "reason": (
                        "No deterministic candidate was found for accepted actual types: "
                        f"{', '.join(expected['accepted_actual_types'])}."
                    ),
                    "requires_review": True,
                }
            )
            status = "missing"
        elif len(strong) == 1 or strong[0]["score"] > strong[1]["score"]:
            selected = strong[0]
            actual_refs = [selected["actual_ref"]]
            observed["actual"] = selected["facts"]
            gaps.append({"code": "actual_node_not_linked", "severity": "partial"})
            actions.append(
                {
                    "action": "link_desired_node_to_actual",
                    "target": _target_ref(desired_node.id, desired_node.name),
                    "actual_ref": selected["actual_ref"],
                    "reason": "A single deterministic actual node candidate was found but is not explicitly linked.",
                    "requires_review": True,
                }
            )
            status = "partial"
        else:
            gaps.append({"code": "ambiguous_actual_node_candidates", "severity": "conflict"})
            actions.append(
                {
                    "action": "link_desired_node_to_actual",
                    "target": _target_ref(desired_node.id, desired_node.name),
                    "candidates": [candidate["actual_ref"] for candidate in strong],
                    "reason": "Multiple actual node candidates matched with the same confidence.",
                    "requires_review": True,
                }
            )
            status = "conflict"

    summary = {
        "target": _target_ref(desired_node.id, desired_node.name),
        "status": status,
        "gap_codes": [gap["code"] for gap in gaps],
        "actual_ref_count": len(actual_refs),
        "candidate_count": len(observed.get("candidates") or []),
        "accepted_actual_types": expected["accepted_actual_types"],
        "evaluation_scope": "node_identity_and_primary_facts",
    }
    return EvaluationResult(
        target_type=NODE_TARGET_TYPE,
        target_id=desired_node.id,
        status=status,
        deterministic_summary=summary,
        actual_refs=actual_refs,
        observed_facts=observed,
        expected_facts=expected,
        gap_summary={"gaps": gaps},
        recommended_actions=actions,
    )


def evaluate_endpoint_intent(
    desired_endpoint: DesiredEndpoint,
    *,
    desired_node: DesiredNode | None = None,
    realized_ip: ActualIPAddress | None = None,
    ip_candidates: Iterable[ActualIPAddress] = (),
    range_candidates: Iterable[DesiredIPRange] | None = None,
    node_evaluation: EvaluationResult | None = None,
    node_realized_device: ActualDevice | None = None,
    node_realized_vm: ActualVirtualMachine | None = None,
    interfaces_by_device_id: Mapping[str, list[ActualInterface]] | None = None,
) -> EvaluationResult:
    """Compare a `DesiredEndpoint` with actual IP and interface facts."""
    interfaces_by_device_id = interfaces_by_device_id or {}
    expected = _expected_endpoint_facts(desired_endpoint)
    actual_refs: list[dict[str, Any]] = []
    observed: dict[str, Any] = {}
    gaps: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []

    if realized_ip is not None:
        actual_refs.append(_actual_ref("ipam.ipaddress", realized_ip))
        observed["actual_ip_address"] = _actual_ip_facts(realized_ip)
        expected_host = _host_address(expected.get("ip_address"))
        actual_host = _host_address(observed["actual_ip_address"].get("address"))
        if expected_host and actual_host and expected_host != actual_host:
            gaps.append(
                {
                    "code": "ip_address_mismatch",
                    "severity": "conflict",
                    "expected": expected_host,
                    "actual": actual_host,
                }
            )
    else:
        matches = _matching_ip_candidates(expected.get("ip_address"), ip_candidates)
        observed["ip_candidates"] = matches
        expected_host = _host_address(expected.get("ip_address"))
        self_observation = _endpoint_ipam_self_observation(node_realized_device, node_realized_vm)
        observed["ipam_self_observation"] = self_observation
        eligibility_basis = _resolve_ipam_eligibility(
            expected.get("ip_policy"), expected_host, self_observation["observed_hosts"]
        )
        observed["ipam_eligibility_basis"] = eligibility_basis
        endpoint_identity = {
            "endpoint_id": desired_endpoint.id,
            "endpoint_name": desired_endpoint.name,
            "ip_policy": expected.get("ip_policy"),
            "ip_address": expected_host,
        }
        if expected.get("ip_address") and eligibility_basis != "eligible":
            gaps.append(
                {
                    "code": f"ipam_reconcile_observation_{eligibility_basis}",
                    "severity": "needs_review",
                    "expected": endpoint_identity,
                    "actual": self_observation,
                }
            )
        elif expected.get("ip_address") and not matches:
            gaps.append(
                {
                    "code": "missing_actual_ip_address",
                    "severity": "partial",
                    "expected": endpoint_identity,
                    "actual": {"ipam_state": "missing", **self_observation},
                }
            )
            actions.append(
                {
                    "action": "create_or_link_ip_address",
                    "target": _target_ref(desired_endpoint.id, desired_endpoint.name),
                    "reason": "No actual IPAddress candidate matches the desired endpoint address.",
                    "requires_review": True,
                }
            )
        elif len(matches) == 1:
            actual_refs.append(matches[0]["actual_ref"])
            gaps.append(
                {
                    "code": "actual_ip_address_not_linked",
                    "severity": "partial",
                    "expected": endpoint_identity,
                    "actual": {
                        "ipam_state": "unlinked",
                        "matching_ip_address": matches[0]["actual_ref"],
                        **self_observation,
                    },
                }
            )
            actions.append(
                {
                    "action": "create_or_link_ip_address",
                    "target": _target_ref(desired_endpoint.id, desired_endpoint.name),
                    "actual_ref": matches[0]["actual_ref"],
                    "reason": "A matching IPAddress exists but the desired endpoint is not explicitly linked.",
                    "requires_review": True,
                }
            )
        elif len(matches) > 1:
            gaps.append({"code": "ambiguous_ip_address_candidates", "severity": "conflict"})

    if range_candidates is not None:
        range_classification = classify_endpoint_ip_ranges(expected.get("ip_address"), range_candidates)
        observed["ip_policy_range_classification"] = range_classification
        observed["matching_ip_policy_ranges"] = range_classification["matching_ranges"]
        gaps.extend(_ip_policy_range_gaps(expected, range_classification))

    interface_candidates = _interface_candidates_for_endpoint(
        desired_node,
        node_realized_device,
        node_realized_vm,
        interfaces_by_device_id,
        node_evaluation,
    )
    observed["interface_candidates"] = interface_candidates
    mac_candidates = [candidate for candidate in interface_candidates if candidate.get("mac_address")]
    observed["dhcp_mac_candidates"] = mac_candidates
    if _wants_dhcp_material(desired_endpoint):
        if not interface_candidates:
            gaps.append({"code": "missing_interface_candidate", "severity": "partial"})
        elif not mac_candidates:
            gaps.append({"code": "missing_mac_address", "severity": "partial"})
        elif len(mac_candidates) > 1:
            gaps.append({"code": "ambiguous_interface", "severity": "partial"})
            actions.append(
                {
                    "action": "select_dhcp_interface",
                    "target": _target_ref(desired_endpoint.id, desired_endpoint.name),
                    "candidates": mac_candidates,
                    "reason": "Multiple MAC-address-bearing interfaces could satisfy this endpoint.",
                    "requires_review": True,
                }
            )

    if any(gap["severity"] == "conflict" for gap in gaps):
        status = "conflict"
    elif gaps:
        status = "partial"
    else:
        status = "satisfied"

    dhcp_blocking_gap_codes = {
        "ambiguous_interface",
        "missing_mac_address",
        "missing_interface_candidate",
        "missing_ip_policy_range",
        "ambiguous_ip_policy_range",
        "ip_policy_range_mismatch",
        "invalid_ip_policy_range",
        "static_endpoint_in_dhcp_pool",
        "dhcp_reserved_endpoint_in_dynamic_pool",
    }
    dhcp_reservation_ready = (
        expected.get("ip_policy") == "dhcp_reserved"
        and bool(_text(expected.get("ip_address")))
        and len(mac_candidates) == 1
        and not any(gap["code"] in dhcp_blocking_gap_codes for gap in gaps)
        and not any(gap["severity"] == "conflict" for gap in gaps)
    )
    summary = {
        "target": _target_ref(desired_endpoint.id, desired_endpoint.name),
        "status": status,
        "gap_codes": [gap["code"] for gap in gaps],
        "actual_ref_count": len(actual_refs),
        "dhcp_mac_candidate_count": len(mac_candidates),
        "dhcp_reservation_ready": dhcp_reservation_ready,
        "evaluation_scope": "endpoint_ip_and_dhcp_mac_candidates",
    }
    return EvaluationResult(
        target_type=ENDPOINT_TARGET_TYPE,
        target_id=desired_endpoint.id,
        status=status,
        deterministic_summary=summary,
        actual_refs=actual_refs,
        observed_facts=observed,
        expected_facts=expected,
        gap_summary={"gaps": gaps},
        recommended_actions=actions,
    )


def evaluate_service_intent(
    desired_service: DesiredService,
    *,
    dependencies: Iterable[DesiredDependency] = (),
    resolved_services_by_id: Mapping[str, DesiredService] | None = None,
    observed_facts: dict[str, Any] | None = None,
    ai_review_enabled: bool = False,
) -> EvaluationResult:
    """Evaluate a `DesiredService` without invoking AI review."""
    resolved_services_by_id = resolved_services_by_id or {}
    dependency_rows = list(dependencies)
    expected = _expected_service_facts(desired_service, dependency_rows, resolved_services_by_id)
    observed = {
        "service_observation_status": "provided" if observed_facts is not None else "unknown",
        "service_facts": observed_facts or {},
        "ai_review": {
            "enabled": bool(ai_review_enabled),
            "executed": False,
        },
    }
    actual_refs: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []

    lifecycle = expected.get("lifecycle")
    if lifecycle in {"deprecated", "retired"}:
        gaps.append({"code": "service_lifecycle_inactive", "severity": "needs_review", "lifecycle": lifecycle})
        actions.append(
            {
                "action": "review_service_lifecycle",
                "target": _target_ref(desired_service.id, desired_service.name),
                "reason": "The desired service lifecycle is inactive.",
                "requires_review": True,
            }
        )
    elif lifecycle in {"", "unknown"}:
        gaps.append({"code": "missing_service_lifecycle", "severity": "unknown"})

    for dependency in expected["dependencies"]:
        if dependency["resolution_status"] != "unresolved":
            continue
        gaps.append({"code": "unresolved_dependency", "severity": "partial", "dependency": dependency})
        actions.append(
            {
                "action": "resolve_service_dependency",
                "target": _target_ref(desired_service.id, desired_service.name),
                "dependency": dependency,
                "reason": "A desired service dependency is unresolved.",
                "requires_review": True,
            }
        )

    if observed_facts is None:
        gaps.append({"code": "service_observed_facts_unknown", "severity": "unknown"})

    status = _status_from_gaps(gaps)
    summary = {
        "target": _target_ref(desired_service.id, desired_service.name),
        "status": status,
        "gap_codes": [gap["code"] for gap in gaps],
        "dependency_counts": expected["dependency_counts"],
        "requirements_present": bool(expected["requirements"]),
        "service_observation_status": observed["service_observation_status"],
        "ai_review_ready": True,
        "ai_review_executed": False,
        "evaluation_scope": "service_lifecycle_requirements_dependencies",
    }
    return EvaluationResult(
        target_type=SERVICE_TARGET_TYPE,
        target_id=desired_service.id,
        status=status,
        deterministic_summary=summary,
        actual_refs=actual_refs,
        observed_facts=observed,
        expected_facts=expected,
        gap_summary={"gaps": gaps},
        recommended_actions=actions,
    )


def _expected_node_facts(desired_node: DesiredNode) -> dict[str, Any]:
    expected_spec = desired_node.expected_spec or {}
    return {
        "name": _text(desired_node.name),
        "slug": _text(desired_node.slug),
        "node_type": _text(desired_node.node_type),
        "accepted_actual_types": _accepted_actual_types_for_node(desired_node),
        "lifecycle": _text(desired_node.lifecycle),
        "role": _text(desired_node.role),
        "expected_spec": expected_spec,
        "hostname": _first_text(expected_spec.get("hostname"), expected_spec.get("host_name")),
        "serial": _first_text(expected_spec.get("serial"), expected_spec.get("serial_number")),
        "uuid": _first_text(expected_spec.get("uuid"), expected_spec.get("node_uuid")),
        "platform": _first_text(expected_spec.get("platform"), expected_spec.get("os")),
    }


def _expected_endpoint_facts(desired_endpoint: DesiredEndpoint) -> dict[str, Any]:
    return {
        "name": _text(desired_endpoint.name),
        "endpoint_type": _text(desired_endpoint.endpoint_type),
        "ip_address": _text(desired_endpoint.ip_address),
        "ip_policy": _text(desired_endpoint.ip_policy),
        "dns_name": _text(desired_endpoint.dns_name),
        "generate_dnsmasq": bool(desired_endpoint.generate_dnsmasq),
        "dnsmasq_record_type": _text(desired_endpoint.dnsmasq_record_type),
    }


def _expected_service_facts(
    desired_service: DesiredService,
    dependencies: list[DesiredDependency],
    resolved_services_by_id: Mapping[str, DesiredService],
) -> dict[str, Any]:
    dependency_facts = [_dependency_facts(dependency, resolved_services_by_id) for dependency in dependencies]
    counts = {"total": len(dependency_facts), "resolved": 0, "unresolved": 0, "external": 0, "ignored": 0, "other": 0}
    for dependency in dependency_facts:
        status = dependency["resolution_status"]
        counts[status if status in counts else "other"] += 1
    return {
        "name": _text(desired_service.name),
        "slug": _text(desired_service.slug),
        "display_name": _text(desired_service.display_name),
        "service_type": _text(desired_service.service_type),
        "lifecycle": _text(desired_service.lifecycle),
        "catalog_namespace": _text(desired_service.catalog_namespace),
        "catalog_metadata_name": _text(desired_service.catalog_metadata_name),
        "requirements": desired_service.requirements or {},
        "dependencies": dependency_facts,
        "dependency_counts": counts,
    }


def _realized_node_objects(
    desired_node: DesiredNode,
    realized_device: ActualDevice | None,
    realized_vm: ActualVirtualMachine | None,
) -> list[tuple[str, Any]]:
    realized = []
    if desired_node.realized_device_id and realized_device is not None:
        realized.append(("dcim.device", realized_device))
    if desired_node.realized_vm_id and realized_vm is not None:
        realized.append(("virtualization.virtualmachine", realized_vm))
    return realized


def _accepted_actual_types_for_node(desired_node: DesiredNode) -> list[str]:
    allowed = {"device", "virtual_machine", "container"}
    actual_types = []
    for item in desired_node.accepted_actual_types or []:
        normalized = _text(item).strip().lower().replace("-", "_")
        if normalized in allowed and normalized not in actual_types:
            actual_types.append(normalized)
    if actual_types:
        return actual_types

    node_type = _text(desired_node.node_type).strip().lower().replace("-", "_")
    defaults = {
        "device": ["device"],
        "virtual_machine": ["virtual_machine"],
        "container": ["container"],
        "service_host": ["device", "virtual_machine", "container"],
    }
    return list(defaults.get(node_type, ["device"]))


def _actual_type_for_object_type(object_type: str) -> str | None:
    return {"dcim.device": "device", "virtualization.virtualmachine": "virtual_machine"}.get(object_type)


def _rank_node_candidates(
    expected: dict[str, Any],
    *,
    device_candidates: Iterable[ActualDevice],
    vm_candidates: Iterable[ActualVirtualMachine],
    interfaces_by_device_id: Mapping[str, list[ActualInterface]],
) -> list[dict[str, Any]]:
    accepted_actual_types = set(expected["accepted_actual_types"])
    candidate_sources: list[tuple[str, Any]] = []
    if "device" in accepted_actual_types:
        candidate_sources.extend(("dcim.device", device) for device in device_candidates)
    if "virtual_machine" in accepted_actual_types:
        candidate_sources.extend(("virtualization.virtualmachine", vm) for vm in vm_candidates)

    candidates = []
    for object_type, actual in candidate_sources:
        facts = _actual_node_facts(object_type, actual, interfaces_by_device_id)
        score, reasons = _node_candidate_score(expected, facts)
        candidates.append(
            {
                "actual_ref": _actual_ref(object_type, actual),
                "facts": facts,
                "match_reasons": reasons,
                "score": score,
            }
        )
    candidates.sort(
        key=lambda candidate: (-candidate["score"], candidate["actual_ref"]["object_type"], candidate["actual_ref"]["name"])
    )
    return candidates


def _node_candidate_score(expected: dict[str, Any], actual: dict[str, Any]) -> tuple[int, list[str]]:
    score = 0
    reasons = []
    expected_names = {
        canonical_node_name(expected.get("name")),
        canonical_node_name(expected.get("slug")),
        canonical_node_name(expected.get("hostname")),
    }
    actual_names = {
        canonical_node_name(actual.get("name")),
        canonical_node_name(actual.get("hostname")),
        canonical_node_name(actual.get("custom_fields", {}).get("hostname")),
        canonical_node_name(actual.get("custom_fields", {}).get("nodeutils_hostname")),
    }
    expected_names.discard("")
    actual_names.discard("")
    if expected_names.intersection(actual_names):
        score += 50
        reasons.append("name_or_hostname")
    for key, weight in (("serial", 80), ("uuid", 80), ("platform", 10)):
        if _norm(expected.get(key)) and _norm(expected.get(key)) == _norm(actual.get(key)):
            score += weight
            reasons.append(key)
    return score, reasons


def _node_mismatches(expected: dict[str, Any], actual: dict[str, Any]) -> list[dict[str, Any]]:
    gaps = []
    for key in ("serial", "uuid", "platform"):
        expected_value = _text(expected.get(key))
        actual_value = _text(actual.get(key))
        if expected_value and actual_value and _norm(expected_value) != _norm(actual_value):
            gaps.append({"code": f"{key}_mismatch", "severity": "conflict", "expected": expected_value, "actual": actual_value})
    expected_hostname = _text(expected.get("hostname"))
    actual_hostname = _first_text(actual.get("hostname"), actual.get("name"))
    if expected_hostname and actual_hostname and canonical_node_name(expected_hostname) != canonical_node_name(actual_hostname):
        gaps.append(
            {"code": "hostname_mismatch", "severity": "conflict", "expected": expected_hostname, "actual": actual_hostname}
        )
    return gaps


def _ip_policy_range_gaps(expected: dict[str, Any], classification: dict[str, Any]) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    ip_address_text = _text(expected.get("ip_address"))
    ip_policy = _text(expected.get("ip_policy"))
    matching_ranges = classification.get("matching_ranges") or []
    invalid_ranges = classification.get("invalid_ranges") or []
    overlapping_matching_ranges = classification.get("overlapping_matching_ranges") or []

    if invalid_ranges:
        gaps.append({"code": "invalid_ip_policy_range", "severity": "partial", "invalid_ranges": invalid_ranges})

    if ip_address_text and not classification.get("endpoint_ip_valid", False):
        gaps.append(
            {"code": "invalid_ip_policy_range", "severity": "partial", "endpoint_ip": classification.get("endpoint_ip")}
        )
        return gaps

    if not ip_address_text:
        return gaps

    if ip_policy in {"static", "dhcp_reserved"} and not matching_ranges:
        gaps.append({"code": "missing_ip_policy_range", "severity": "partial"})
        return gaps

    if len(matching_ranges) > 1 or overlapping_matching_ranges:
        gaps.append(
            {
                "code": "ambiguous_ip_policy_range",
                "severity": "partial",
                "matching_ranges": matching_ranges,
                "overlapping_ranges": overlapping_matching_ranges,
            }
        )

    matching_policies = {range_fact.get("range_policy") for range_fact in matching_ranges}
    if ip_policy == "dhcp_reserved":
        if "dhcp_dynamic_pool" in matching_policies:
            gaps.append(
                {
                    "code": "dhcp_reserved_endpoint_in_dynamic_pool",
                    "severity": "partial",
                    "matching_ranges": [rf for rf in matching_ranges if rf.get("range_policy") == "dhcp_dynamic_pool"],
                }
            )
        if matching_policies and "dhcp_reservable_pool" not in matching_policies:
            gaps.append(
                {
                    "code": "ip_policy_range_mismatch",
                    "severity": "partial",
                    "ip_policy": ip_policy,
                    "matching_range_policies": sorted(_text(policy) for policy in matching_policies),
                }
            )
    elif ip_policy == "static":
        dhcp_pool_ranges = [
            rf for rf in matching_ranges if rf.get("range_policy") in {"dhcp_reservable_pool", "dhcp_dynamic_pool"}
        ]
        if dhcp_pool_ranges:
            gaps.append({"code": "static_endpoint_in_dhcp_pool", "severity": "partial", "matching_ranges": dhcp_pool_ranges})
        if matching_policies and not matching_policies.intersection({"static_pool", "excluded"}):
            gaps.append(
                {
                    "code": "ip_policy_range_mismatch",
                    "severity": "partial",
                    "ip_policy": ip_policy,
                    "matching_range_policies": sorted(_text(policy) for policy in matching_policies),
                }
            )
    elif ip_policy == "external" and matching_ranges:
        gaps.append({"code": "ip_policy_range_mismatch", "severity": "partial", "ip_policy": ip_policy, "matching_ranges": matching_ranges})
    elif not ip_policy:
        gaps.append({"code": "missing_ip_policy_range", "severity": "partial"})

    return gaps


def _actual_node_facts(
    object_type: str, actual: Any, interfaces_by_device_id: Mapping[str, list[ActualInterface]]
) -> dict[str, Any]:
    if isinstance(actual, ActualDevice):
        custom_fields = actual.facts or {}
        interfaces = interfaces_by_device_id.get(actual.id, [])
        primary_mac_address = _normalize_mac(
            _first_text(
                custom_fields.get("primary_mac_address"), custom_fields.get("primary_mac"), custom_fields.get("mac_address")
            )
        )
        return {
            "object_type": object_type,
            "id": actual.id,
            "name": _text(actual.name),
            "hostname": _first_text(custom_fields.get("hostname"), custom_fields.get("nodeutils_hostname")),
            "serial": _first_text(actual.serial, custom_fields.get("serial"), custom_fields.get("serial_number")),
            "uuid": _first_text(custom_fields.get("uuid"), custom_fields.get("node_uuid")),
            "platform": _first_text(actual.platform, custom_fields.get("platform"), custom_fields.get("os")),
            "primary_mac_address": primary_mac_address,
            "custom_fields": custom_fields,
            "interfaces": [_interface_facts(object_type, actual, interface) for interface in interfaces],
            "interface_count": len(interfaces),
        }
    # ActualVirtualMachine: only id/name are fetched by Step 1 (no current
    # consumer needs VM custom fields or interfaces).
    return {
        "object_type": object_type,
        "id": actual.id,
        "name": _text(actual.name),
        "hostname": "",
        "serial": "",
        "uuid": "",
        "platform": "",
        "primary_mac_address": "",
        "custom_fields": {},
        "interfaces": [],
        "interface_count": 0,
    }


def _actual_ip_facts(actual_ip: ActualIPAddress) -> dict[str, Any]:
    return {
        "object_type": "ipam.ipaddress",
        "id": actual_ip.id,
        "address": _ip_address_display(actual_ip),
        "dns_name": _text(actual_ip.dns_name),
    }


def _dependency_facts(dependency: DesiredDependency, resolved_services_by_id: Mapping[str, DesiredService]) -> dict[str, Any]:
    facts = {
        "dependency_kind": _text(dependency.dependency_kind),
        "namespace": _text(dependency.namespace),
        "name": _text(dependency.name),
        "raw_ref": _text(dependency.raw_ref),
        "dependency_type": _text(dependency.dependency_type),
        "resolution_status": _text(dependency.resolution_status) or "unresolved",
    }
    resolved_service = resolved_services_by_id.get(dependency.resolved_service_id or "")
    if resolved_service is not None:
        facts["resolved_service"] = _target_ref(resolved_service.id, resolved_service.name)
    return facts


def _matching_ip_candidates(ip_address: Any, ip_candidates: Iterable[ActualIPAddress]) -> list[dict[str, Any]]:
    expected = _host_address(ip_address)
    if not expected:
        return []
    matches = []
    for actual in ip_candidates:
        actual_host = _host_address(_ip_address_display(actual))
        if expected == actual_host:
            matches.append({"actual_ref": _actual_ref("ipam.ipaddress", actual), "facts": _actual_ip_facts(actual)})
    matches.sort(key=lambda match: match["actual_ref"]["name"])
    return matches


def _interface_candidates_for_endpoint(
    desired_node: DesiredNode | None,
    node_realized_device: ActualDevice | None,
    node_realized_vm: ActualVirtualMachine | None,
    interfaces_by_device_id: Mapping[str, list[ActualInterface]],
    node_evaluation: EvaluationResult | None,
) -> list[dict[str, Any]]:
    actual_objects: list[tuple[str, Any]] = []
    if desired_node is not None:
        actual_objects = _realized_node_objects(desired_node, node_realized_device, node_realized_vm)

    candidates = []
    for object_type, actual_node in actual_objects:
        for interface in interfaces_by_device_id.get(getattr(actual_node, "id", None), []):
            candidates.append(_interface_facts(object_type, actual_node, interface))
    if not any(candidate.get("mac_address") for candidate in candidates):
        for object_type, actual_node in actual_objects:
            primary_candidate = _primary_mac_candidate(object_type, actual_node)
            if primary_candidate:
                candidates.append(primary_candidate)

    if candidates:
        return sorted(candidates, key=_interface_sort_key)

    if node_evaluation is not None:
        observed = node_evaluation.observed_facts
        actual = observed.get("actual")
        if isinstance(actual, dict):
            for interface in actual.get("interfaces") or []:
                if isinstance(interface, dict):
                    candidates.append(interface)
            if not any(candidate.get("mac_address") for candidate in candidates):
                primary_candidate = _primary_mac_candidate_from_facts(actual)
                if primary_candidate:
                    candidates.append(primary_candidate)
    return sorted(candidates, key=_interface_sort_key)


def _interface_facts(object_type: str, actual_node: Any, interface: ActualInterface) -> dict[str, Any]:
    return {
        "actual_node_ref": _actual_ref(object_type, actual_node),
        "interface_id": interface.id,
        "interface_name": _text(interface.name),
        "mac_address": _normalize_mac(interface.mac_address),
        "enabled": bool(interface.enabled),
    }


def _primary_mac_candidate(object_type: str, actual_node: Any) -> dict[str, Any]:
    if not isinstance(actual_node, ActualDevice):
        return {}
    custom_fields = actual_node.facts or {}
    mac_address = _normalize_mac(
        _first_text(custom_fields.get("primary_mac_address"), custom_fields.get("primary_mac"), custom_fields.get("mac_address"))
    )
    if not mac_address:
        return {}
    return {
        "actual_node_ref": _actual_ref(object_type, actual_node),
        "interface_id": "",
        "interface_name": "primary_mac_address",
        "mac_address": mac_address,
        "enabled": True,
    }


def _primary_mac_candidate_from_facts(actual: dict[str, Any]) -> dict[str, Any]:
    custom_fields = actual.get("custom_fields") if isinstance(actual.get("custom_fields"), dict) else {}
    mac_address = _normalize_mac(
        _first_text(
            actual.get("primary_mac_address"),
            custom_fields.get("primary_mac_address"),
            custom_fields.get("primary_mac"),
            custom_fields.get("mac_address"),
        )
    )
    if not mac_address:
        return {}
    actual_ref = {
        "object_type": _text(actual.get("object_type")),
        "id": _text(actual.get("id")),
        "name": _text(actual.get("name")),
    }
    return {"actual_node_ref": actual_ref, "interface_id": "", "interface_name": "primary_mac_address", "mac_address": mac_address, "enabled": True}


def _wants_dhcp_material(desired_endpoint: DesiredEndpoint) -> bool:
    return (
        _text(desired_endpoint.ip_policy) == "dhcp_reserved"
        and bool(desired_endpoint.generate_dnsmasq)
        and bool(_text(desired_endpoint.ip_address))
    )


def _status_from_gaps(gaps: list[dict[str, Any]]) -> str:
    severities = {gap.get("severity") for gap in gaps}
    if "conflict" in severities:
        return "conflict"
    if "missing" in severities:
        return "missing"
    if "partial" in severities:
        return "partial"
    if "needs_review" in severities:
        return "needs_review"
    if "unknown" in severities:
        return "unknown"
    return "satisfied"


def _actual_ref(object_type: str, obj: Any) -> dict[str, Any]:
    name = getattr(obj, "name", None)
    if not name and object_type == "ipam.ipaddress":
        name = _ip_address_display(obj)
    return {"object_type": object_type, "id": getattr(obj, "id", ""), "name": _text(name)}


def _target_ref(obj_id: str, name: str | None) -> dict[str, Any]:
    return {"id": obj_id, "name": _text(name)}


def _host_address(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    try:
        return str(ip_interface(text).ip)
    except ValueError:
        return text.split("/", maxsplit=1)[0]


def _endpoint_ipam_self_observation(
    node_realized_device: ActualDevice | None, node_realized_vm: ActualVirtualMachine | None
) -> dict[str, Any]:
    """Self-observation evidence for the non-`dhcp_reserved` IPAM eligibility gate.

    Reads `ActualDevice.actual_facts().local_ip` -- the same
    `primary_ip_address` custom field nauto's ingest Job writes -- never the
    controller-local nodeutils cache. `ActualVirtualMachine` (Step 1) carries
    no custom fields, so a linked VM contributes nothing here rather than a
    guessed value; this is a structural fact of the current actual-source
    schema, not a special case.
    """

    candidates: list[dict[str, Any]] = []
    if node_realized_device is not None:
        facts = node_realized_device.actual_facts()
        host = _host_address(facts.local_ip)
        if host:
            candidates.append(
                {"basis": "realized_device.primary_ip_address", "host": host, "last_seen": facts.collected_at}
            )
    return {"candidates": candidates, "observed_hosts": sorted({c["host"] for c in candidates})}


def _resolve_ipam_eligibility(ip_policy: Any, desired_host: str, observed_hosts: list[str]) -> str:
    """Return `"eligible"`, `"missing"`, `"mismatch"`, or `"ambiguous"`.

    `dhcp_reserved` is always eligible without an observation (unchanged
    reservation-intent behavior); `static`/`external` require exactly one
    distinct observed host address matching the normalized desired host.
    """

    if _text(ip_policy) == "dhcp_reserved":
        return "eligible"
    if not observed_hosts:
        return "missing"
    if len(observed_hosts) > 1:
        return "ambiguous"
    return "eligible" if observed_hosts[0] == desired_host else "mismatch"


def _ip_address_display(actual_ip: ActualIPAddress) -> str:
    host = _text(actual_ip.host)
    mask_length = actual_ip.mask_length
    if host and mask_length is not None:
        return f"{host}/{mask_length}"
    return host


def _strict_host_address(value: Any) -> Any | None:
    text = _text(value)
    if not text:
        return None
    try:
        return ip_interface(text).ip if "/" in text else _parse_ip_address(text)
    except ValueError:
        return None


def _classified_ip_ranges(range_candidates: Iterable[DesiredIPRange]) -> dict[str, list[Any]]:
    valid_ranges: list[NormalizedIPRange] = []
    invalid_ranges: list[dict[str, Any]] = []
    for ip_range in range_candidates:
        facts = desired_ip_range_facts(ip_range)
        normalized = normalize_desired_range_addresses(ip_range)
        if not normalized["valid"]:
            invalid_ranges.append({"facts": facts})
            continue

        start_ip = _strict_host_address(normalized["start_address"])
        end_ip = _strict_host_address(normalized["end_address"])
        if start_ip is None or end_ip is None:
            invalid_facts = {**facts, "valid": False, "errors": ["invalid_range_normalization"]}
            invalid_ranges.append({"facts": invalid_facts})
            continue

        valid_ranges.append(
            NormalizedIPRange(
                source=ip_range,
                facts=facts,
                start_ip=start_ip,
                end_ip=end_ip,
                sort_key=_range_sort_key(facts, start_ip, end_ip),
            )
        )

    valid_ranges.sort(key=lambda entry: entry.sort_key)
    invalid_ranges.sort(key=lambda entry: _invalid_range_sort_key(entry["facts"]))
    return {"valid": valid_ranges, "invalid": invalid_ranges}


def _overlap_records(valid_ranges: list[NormalizedIPRange]) -> list[dict[str, Any]]:
    records = []
    sorted_ranges = sorted(valid_ranges, key=lambda entry: entry.sort_key)
    for index, first in enumerate(sorted_ranges):
        for second in sorted_ranges[index + 1 :]:
            if first.start_ip.version != second.start_ip.version:
                continue
            if int(second.start_ip) > int(first.end_ip):
                break
            if int(first.start_ip) <= int(second.end_ip) and int(second.start_ip) <= int(first.end_ip):
                records.append(
                    {
                        "first": first.facts,
                        "second": second.facts,
                        "overlap_start_address": str(max(first.start_ip, second.start_ip)),
                        "overlap_end_address": str(min(first.end_ip, second.end_ip)),
                    }
                )
    records.sort(
        key=lambda record: (
            _range_identity_key(record["first"]),
            _range_identity_key(record["second"]),
            record["overlap_start_address"],
            record["overlap_end_address"],
        )
    )
    return records


def _range_sort_key(facts: dict[str, Any], start_ip: Any, end_ip: Any) -> tuple[int, int, int, str, str, str]:
    return (
        start_ip.version,
        int(start_ip),
        int(end_ip),
        _text(facts.get("slug")),
        _text(facts.get("name")),
        _text(facts.get("desired_ip_range_id")),
    )


def _invalid_range_sort_key(facts: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        _text(facts.get("start_address")),
        _text(facts.get("end_address")),
        _text(facts.get("slug")),
        _text(facts.get("name")),
        _text(facts.get("desired_ip_range_id")),
    )


def _range_identity_key(facts: dict[str, Any]) -> tuple[str, str, str]:
    return (_text(facts.get("desired_ip_range_id")), _text(facts.get("slug")), _text(facts.get("name")))


def _normalize_mac(value: Any) -> str:
    text = re.sub(r"[^0-9A-Fa-f]", "", _text(value))
    if len(text) != 12:
        return ""
    return ":".join(text[index : index + 2].lower() for index in range(0, 12, 2))


def _first_text(*values: Any) -> str:
    for value in values:
        text = _text(value)
        if text:
            return text
    return ""


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _norm(value: Any) -> str:
    return _text(value).lower()


def _interface_sort_key(candidate: dict[str, Any]) -> tuple[str, str, str]:
    return (
        _text(candidate.get("actual_node_ref", {}).get("name")),
        _text(candidate.get("interface_name")),
        _text(candidate.get("mac_address")),
    )
