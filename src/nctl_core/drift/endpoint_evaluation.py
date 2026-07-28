"""Deterministic desired-endpoint drift evaluation and IP/MAC candidates."""

from __future__ import annotations

from ipaddress import ip_interface
from typing import TYPE_CHECKING, Any, Iterable, Mapping

from .evaluation import ENDPOINT_TARGET_TYPE, EvaluationResult, _actual_ref, _first_text, _ip_address_display, _norm, _target_ref, _text
from .ip_ranges import classify_endpoint_ip_ranges
from .interfaces import normalize_mac
from .node_evaluation import _actual_node_facts, _realized_node_objects

if TYPE_CHECKING:
    from nctl_core.sources.actual import ActualDevice, ActualIPAddress, ActualInterface
    from nctl_core.sources.desired import DesiredEndpoint, DesiredIPRange, DesiredNode
def evaluate_endpoint_intent(
    desired_endpoint: DesiredEndpoint,
    *,
    desired_node: DesiredNode | None = None,
    realized_ip: ActualIPAddress | None = None,
    ip_candidates: Iterable[ActualIPAddress] = (),
    range_candidates: Iterable[DesiredIPRange] | None = None,
    node_evaluation: EvaluationResult | None = None,
    node_realized_device: ActualDevice | None = None,
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
        self_observation = _endpoint_ipam_self_observation(node_realized_device)
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

    # VM p3 Step 6: compare a desired endpoint's own MAC (`DesiredEndpoint.
    # mac_address`, already canonicalized by the Step 1 read-model) against
    # the interface-derived actual candidates computed just above -- the one
    # shared computation both `drift/comparators.py` (drift/reconcile) and
    # `dnsmasq_query.py`'s renderer path consume via `deterministic_summary`/
    # `observed_facts`, so they can never disagree about a given endpoint's
    # desired-MAC status.
    desired_mac = _text(desired_endpoint.mac_address)
    desired_mac_status = ""
    if desired_mac:
        if not mac_candidates:
            desired_mac_status = "desired_only"
        elif len(mac_candidates) == 1:
            actual_mac = _text(mac_candidates[0].get("mac_address"))
            if _norm(actual_mac) == _norm(desired_mac):
                desired_mac_status = "agree"
            else:
                desired_mac_status = "mismatch"
                gaps.append(
                    {
                        "code": "desired_mac_mismatch",
                        "severity": "conflict",
                        "expected": desired_mac,
                        "actual": actual_mac,
                    }
                )
        else:
            desired_mac_status = "ambiguous_actual"

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
        "desired_mac_mismatch",
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
        "desired_mac_status": desired_mac_status,
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

def _actual_ip_facts(actual_ip: ActualIPAddress) -> dict[str, Any]:
    return {
        "object_type": "ipam.ipaddress",
        "id": actual_ip.id,
        "address": _ip_address_display(actual_ip),
        "dns_name": _text(actual_ip.dns_name),
    }

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
    interfaces_by_device_id: Mapping[str, list[ActualInterface]],
    node_evaluation: EvaluationResult | None,
) -> list[dict[str, Any]]:
    actual_objects: list[tuple[str, Any]] = []
    if desired_node is not None:
        actual_objects = _realized_node_objects(desired_node, node_realized_device)

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
        "mac_address": normalize_mac(interface.mac_address),
        "enabled": bool(interface.enabled),
    }


def _primary_mac_candidate(object_type: str, actual_node: Any) -> dict[str, Any]:
    if object_type != "dcim.device":
        return {}
    custom_fields = actual_node.facts or {}
    mac_address = normalize_mac(
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
    mac_address = normalize_mac(
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

def _host_address(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    try:
        return str(ip_interface(text).ip)
    except ValueError:
        return text.split("/", maxsplit=1)[0]


def _endpoint_ipam_self_observation(node_realized_device: ActualDevice | None) -> dict[str, Any]:
    """Self-observation evidence for the non-`dhcp_reserved` IPAM eligibility gate.

    Reads `ActualDevice.actual_facts().local_ip` -- the same
    `primary_ip_address` custom field nauto's ingest Job writes -- never the
    controller-local nodeutils cache. Guest-OS realization is Device-only
    (VM p3 Step 5 removed `DesiredNode.realized_vm`), so there is no VM
    self-observation branch here.
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

def _interface_sort_key(candidate: dict[str, Any]) -> tuple[str, str, str]:
    return (
        _text(candidate.get("actual_node_ref", {}).get("name")),
        _text(candidate.get("interface_name")),
        _text(candidate.get("mac_address")),
    )
