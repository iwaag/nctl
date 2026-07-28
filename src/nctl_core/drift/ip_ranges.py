"""Pure deterministic IP-range rules for drift evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address as _parse_ip_address, ip_interface
from typing import Any, Iterable

from nctl_core.sources.desired import DesiredIPRange

@dataclass(frozen=True)
class NormalizedIPRange:
    source: DesiredIPRange
    facts: dict[str, Any]
    start_ip: Any
    end_ip: Any
    sort_key: tuple[int, int, int, str, str, str]



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



def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()



