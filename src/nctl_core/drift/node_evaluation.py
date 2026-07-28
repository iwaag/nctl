"""Deterministic desired-node drift evaluation and candidate ranking."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterable, Mapping

from nctl_core.names import canonical_node_name

from .evaluation import NODE_TARGET_TYPE, EvaluationResult, _actual_ref, _first_text, _norm, _target_ref, _text
from .interfaces import normalize_mac

if TYPE_CHECKING:
    from nctl_core.sources.actual import ActualDevice, ActualInterface, ActualVirtualMachine
    from nctl_core.sources.desired import DesiredNode
def evaluate_node_intent(
    desired_node: DesiredNode,
    *,
    device_candidates: Iterable[ActualDevice] = (),
    vm_candidates: Iterable[ActualVirtualMachine] = (),
    interfaces_by_device_id: Mapping[str, list[ActualInterface]] | None = None,
    realized_device: ActualDevice | None = None,
) -> EvaluationResult:
    """Compare a `DesiredNode` with actual Device/VM candidates."""
    interfaces_by_device_id = interfaces_by_device_id or {}
    expected = _expected_node_facts(desired_node)
    accepted_actual_types = set(expected["accepted_actual_types"])
    realized = _realized_node_objects(desired_node, realized_device)
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

def _realized_node_objects(
    desired_node: DesiredNode,
    realized_device: ActualDevice | None,
) -> list[tuple[str, Any]]:
    realized = []
    if desired_node.realized_device_id and realized_device is not None:
        realized.append(("dcim.device", realized_device))
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

def _actual_node_facts(
    object_type: str, actual: Any, interfaces_by_device_id: Mapping[str, list[ActualInterface]]
) -> dict[str, Any]:
    if object_type == "dcim.device":
        custom_fields = actual.facts or {}
        interfaces = interfaces_by_device_id.get(actual.id, [])
        primary_mac_address = normalize_mac(
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

def _interface_facts(object_type: str, actual_node: Any, interface: ActualInterface) -> dict[str, Any]:
    return {
        "actual_node_ref": _actual_ref(object_type, actual_node),
        "interface_id": interface.id,
        "interface_name": _text(interface.name),
        "mac_address": normalize_mac(interface.mac_address),
        "enabled": bool(interface.enabled),
    }
