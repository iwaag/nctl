"""Deterministic dnsmasq renderer (pure functions, mapping-based).

Ported from `nintent`'s `nautobot_intent_catalog/dnsmasq.py`. Record lines, sort
keys, the `skip_reasons` vocabulary, and summary counts are byte-identical to
the source; only input access changed from `getattr`-on-ORM to plain mappings
(the shape the Phase 2 GraphQL fetch layer returns).
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from ipaddress import ip_interface
import json
import re
from typing import Any, Iterable, Mapping


ELIGIBLE_NODE_LIFECYCLES = frozenset({"planned", "approved", "active"})
ELIGIBLE_ENDPOINT_TYPES = frozenset({"primary", "management", "service", "vpn"})
SUPPORTED_RECORD_TYPES = frozenset({"host_record", "address", "cname"})
# fix_sshkey3 Step 3: bumped 4.0 -> 5.0, a deliberate breaking byte-contract
# change -- render_dnsmasq_records_conf() no longer embeds generated_at/
# operation_id in the deployed bytes (see dnsmasq_content_sha256()).
DNSMASQ_EXPORT_SCHEMA_VERSION = "5.0"


@dataclass(frozen=True)
class DnsmasqExport:
    """Serializable dnsmasq export payload."""

    summary: dict[str, Any]
    dns_records: list[dict[str, Any]]
    dhcp_reservations: list[dict[str, Any]]
    dhcp_ranges: list[dict[str, Any]]
    skipped: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "dns_records": self.dns_records,
            "dhcp_reservations": self.dhcp_reservations,
            "dhcp_ranges": self.dhcp_ranges,
            "skipped": self.skipped,
        }


def export_dnsmasq_records(
    endpoints: Iterable[Mapping[str, Any]],
    *,
    ip_ranges: Iterable[Mapping[str, Any]] = (),
    endpoint_evaluations: Mapping[str, Any] | None = None,
    node_evaluations: Mapping[str, Any] | None = None,
    include_skipped: bool = True,
) -> DnsmasqExport:
    """Return deterministic DNS records, DHCP reservations, and DHCP ranges."""

    dns_records: list[dict[str, Any]] = []
    dhcp_reservations: list[dict[str, Any]] = []
    dhcp_ranges: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    dns_skipped_count = 0
    dhcp_skipped_count = 0
    range_skipped_count = 0
    total_count = 0
    total_range_count = 0

    for endpoint in endpoints:
        total_count += 1
        dns_skip_reasons = _dns_skip_reasons(endpoint)
        if dns_skip_reasons:
            dns_skipped_count += 1
            if include_skipped:
                skipped.append(_skip_entry(endpoint, "dns_record", dns_skip_reasons))
        else:
            dns_records.append(_dns_record_entry(endpoint))

        endpoint_evaluation = _evaluation_for(endpoint, endpoint_evaluations)
        desired_node = _mapping(endpoint.get("desired_node"))
        node_evaluation = _evaluation_for(desired_node, node_evaluations)
        reservation = resolve_dhcp_reservation(
            endpoint,
            endpoint_evaluation=endpoint_evaluation,
            node_evaluation=node_evaluation,
        )
        if reservation["skip_reasons"]:
            dhcp_skipped_count += 1
            if include_skipped:
                skipped.append(_skip_entry(endpoint, "dhcp_reservation", reservation["skip_reasons"]))
        else:
            dhcp_reservations.append(reservation)

    for ip_range in ip_ranges:
        total_range_count += 1
        range_entry = _dhcp_range_entry(ip_range)
        if range_entry["skip_reasons"]:
            range_skipped_count += 1
            if include_skipped:
                skipped.append(_range_skip_entry(ip_range, range_entry["skip_reasons"]))
        else:
            dhcp_ranges.append(range_entry)

    dns_records.sort(key=_dns_record_sort_key)
    dhcp_reservations.sort(key=_dhcp_reservation_sort_key)
    dhcp_ranges.sort(key=_dhcp_range_sort_key)
    skipped.sort(key=_skip_sort_key)
    skipped_endpoint_details = sum(1 for entry in skipped if entry.get("item_type") in {"dns_record", "dhcp_reservation"})
    skipped_range_details = sum(1 for entry in skipped if entry.get("item_type") == "dhcp_range")
    summary = {
        "dns_records": len(dns_records),
        "dhcp_reservations": len(dhcp_reservations),
        "dhcp_ranges": len(dhcp_ranges),
        "eligible_endpoints": len(dns_records),
        "eligible_ranges": len(dhcp_ranges),
        "record_types": {
            "address": sum(1 for record in dns_records if record["record_type"] == "address"),
            "cname": sum(1 for record in dns_records if record["record_type"] == "cname"),
            "host_record": sum(1 for record in dns_records if record["record_type"] == "host_record"),
        },
        "skipped": {
            "details": len(skipped),
            "dhcp_reservations": dhcp_skipped_count,
            "dhcp_ranges": range_skipped_count,
            "dns_records": dns_skipped_count,
        },
        "skipped_endpoint_details": skipped_endpoint_details,
        "skipped_endpoints": dns_skipped_count,
        "skipped_range_details": skipped_range_details,
        "skipped_ranges": range_skipped_count,
        "total_endpoints": total_count,
        "total_ranges": total_range_count,
    }
    return DnsmasqExport(
        summary=summary,
        dns_records=dns_records,
        dhcp_reservations=dhcp_reservations,
        dhcp_ranges=dhcp_ranges,
        skipped=skipped,
    )


def resolve_dhcp_reservation(
    endpoint: Mapping[str, Any],
    *,
    endpoint_evaluation: Mapping[str, Any] | None = None,
    node_evaluation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return one DHCP reservation entry or a skipped entry for a desired endpoint."""

    skip_reasons = _dhcp_skip_reasons(endpoint)
    endpoint_data = _evaluation_data(endpoint_evaluation)
    node_data = _evaluation_data(node_evaluation)
    endpoint_summary = _mapping(endpoint_data.get("deterministic_summary"))
    endpoint_observed = _mapping(endpoint_data.get("observed_facts"))
    node_actual_refs = _list(node_data.get("actual_refs"))
    mac_candidates = [
        candidate
        for candidate in _list(endpoint_observed.get("dhcp_mac_candidates"))
        if isinstance(candidate, dict)
    ]
    actual_refs = _unique_actual_refs(mac_candidates, node_actual_refs)
    normalized_mac_candidates = []

    if not endpoint_data:
        skip_reasons.append("missing_endpoint_evaluation")
    if endpoint_summary and endpoint_summary.get("dhcp_reservation_ready") is False:
        skip_reasons.append("endpoint_evaluation_not_dhcp_ready")
    if len(actual_refs) != 1:
        skip_reasons.append("missing_actual_node" if not actual_refs else "ambiguous_actual_node")

    for candidate in mac_candidates:
        mac_address = _normalize_mac(candidate.get("mac_address"))
        if mac_address:
            normalized = {**candidate, "mac_address": mac_address}
            normalized_mac_candidates.append(normalized)

    if not mac_candidates:
        skip_reasons.append("missing_mac_address")
    elif not normalized_mac_candidates:
        skip_reasons.append("invalid_mac_address")
    elif len({candidate["mac_address"] for candidate in normalized_mac_candidates}) != 1:
        skip_reasons.append("ambiguous_interface")

    desired_node = _mapping(endpoint.get("desired_node"))
    dns_name = _text(endpoint.get("dns_name"))
    ip_address = _host_address(_text(endpoint.get("ip_address")))
    mac_address = normalized_mac_candidates[0]["mac_address"] if normalized_mac_candidates else ""
    actual_ref = actual_refs[0] if len(actual_refs) == 1 else {}
    line = f"dhcp-host={mac_address},{dns_name},{ip_address}" if not skip_reasons else ""
    return {
        "actual_ref": actual_ref,
        "confidence": "deterministic" if not skip_reasons else "none",
        "desired_endpoint": _text(endpoint.get("name")),
        "desired_endpoint_id": _pk(endpoint),
        "desired_node": _text(desired_node.get("name")),
        "desired_node_id": _pk(desired_node),
        "desired_node_slug": _text(desired_node.get("slug")),
        "dns_name": dns_name,
        "endpoint_type": _text(endpoint.get("endpoint_type")),
        "ip_address": ip_address,
        "ip_policy": _text(endpoint.get("ip_policy")),
        "line": line,
        "mac_address": mac_address,
        "skip_reasons": sorted(set(skip_reasons)),
    }


def render_dnsmasq_records_conf(export: DnsmasqExport) -> str:
    """Return dnsmasq configuration text for a generated export.

    fix_sshkey3 Step 3: byte-deterministic in the source state alone -- no
    `generated_at`/`operation_id` (those remain in the JSON envelope, event
    log, and artifact metadata only, via `dnsmasq_export_payload`). Equal
    `DnsmasqExport` values at different times or under different operation
    IDs must produce byte-identical conf text and therefore the same
    `dnsmasq_content_sha256`; re-rendering unchanged desired state must
    never manufacture drift.
    """

    lines = [
        "# Generated by nctl",
        f"# schema_version: {DNSMASQ_EXPORT_SCHEMA_VERSION}",
    ]
    lines.extend(record["line"] for record in export.dns_records)
    lines.extend(reservation["line"] for reservation in export.dhcp_reservations)
    lines.extend(ip_range["line"] for ip_range in export.dhcp_ranges)
    return "\n".join(lines) + "\n"


def dnsmasq_content_sha256(conf: str) -> str:
    """Return the lowercase hex SHA-256 of the exact UTF-8 conf bytes.

    The one standard, full-file digest algorithm for the deployed dnsmasq
    conf (fix_sshkey3 Step 3) -- nodeutils computes the same standard
    full-file SHA-256 over the bytes it reads from disk, with no
    comment-stripping, sidecar acknowledgment, or second canonicalizer
    anywhere in the pipeline.
    """

    return hashlib.sha256(conf.encode("utf-8")).hexdigest()


def dnsmasq_export_payload(
    export: DnsmasqExport,
    *,
    generated_at: str,
    operation_id: str | None = None,
) -> dict[str, Any]:
    """Return a stable, machine-readable dnsmasq export payload."""

    return {
        "schema_version": DNSMASQ_EXPORT_SCHEMA_VERSION,
        "generated_at": generated_at,
        "operation_id": operation_id,
        "summary": export.summary,
        "dns_records": export.dns_records,
        "dhcp_reservations": export.dhcp_reservations,
        "dhcp_ranges": export.dhcp_ranges,
        "skipped": export.skipped,
    }


def render_dnsmasq_export_json(
    export: DnsmasqExport,
    *,
    generated_at: str,
    operation_id: str | None = None,
) -> str:
    """Return a deterministic JSON representation of a generated export."""

    return json.dumps(
        dnsmasq_export_payload(export, generated_at=generated_at, operation_id=operation_id),
        sort_keys=True,
        ensure_ascii=True,
        indent=2,
    ) + "\n"


def _dns_skip_reasons(endpoint: Mapping[str, Any]) -> list[str]:
    reasons = _base_skip_reasons(endpoint)
    record_type = _text(endpoint.get("dnsmasq_record_type"))
    if record_type not in SUPPORTED_RECORD_TYPES:
        reasons.append("dnsmasq_record_type_not_supported")
    if record_type == "cname" and not _text(endpoint.get("vpn_dns_name")):
        reasons.append("missing_cname_alias")
    return reasons


def _dhcp_skip_reasons(endpoint: Mapping[str, Any]) -> list[str]:
    reasons = _base_skip_reasons(endpoint)
    if _text(endpoint.get("ip_policy")) != "dhcp_reserved":
        reasons.append("ip_policy_not_dhcp_reserved")
    return reasons


def _base_skip_reasons(endpoint: Mapping[str, Any]) -> list[str]:
    reasons = []
    desired_node = _mapping(endpoint.get("desired_node"))
    lifecycle = _text(desired_node.get("lifecycle"))
    endpoint_type = _text(endpoint.get("endpoint_type"))

    if not bool(endpoint.get("generate_dnsmasq", False)):
        reasons.append("generate_dnsmasq_false")
    if not _text(endpoint.get("ip_address")):
        reasons.append("missing_ip_address")
    if not _text(endpoint.get("dns_name")):
        reasons.append("missing_dns_name")
    if lifecycle not in ELIGIBLE_NODE_LIFECYCLES:
        reasons.append("node_lifecycle_not_exportable")
    if endpoint_type not in ELIGIBLE_ENDPOINT_TYPES:
        reasons.append("endpoint_type_not_exportable")
    return reasons


def _dns_record_entry(endpoint: Mapping[str, Any]) -> dict[str, Any]:
    record_type = _text(endpoint.get("dnsmasq_record_type"))
    dns_name = _text(endpoint.get("dns_name"))
    ip_address = _host_address(_text(endpoint.get("ip_address")))
    desired_node = _mapping(endpoint.get("desired_node"))
    vpn_dns_name = _text(endpoint.get("vpn_dns_name"))

    if record_type == "address":
        line = f"address=/{dns_name}/{ip_address}"
        record_name = dns_name
        record_value = ip_address
    elif record_type == "cname":
        line = f"cname={vpn_dns_name},{dns_name}"
        record_name = vpn_dns_name
        record_value = dns_name
    else:
        record_type = "host_record"
        line = f"host-record={dns_name},{ip_address}"
        record_name = dns_name
        record_value = ip_address

    return {
        "desired_endpoint_id": _pk(endpoint),
        "desired_node": _text(desired_node.get("name")),
        "desired_node_id": _pk(desired_node),
        "desired_node_slug": _text(desired_node.get("slug")),
        "dns_name": dns_name,
        "endpoint_name": _text(endpoint.get("name")),
        "endpoint_type": _text(endpoint.get("endpoint_type")),
        "ip_address": ip_address,
        "ip_policy": _text(endpoint.get("ip_policy")),
        "line": line,
        "mdns_name": _text(endpoint.get("mdns_name")),
        "record_name": record_name,
        "record_type": record_type,
        "record_value": record_value,
        "vpn_dns_name": vpn_dns_name,
    }


def _dhcp_range_entry(ip_range: Mapping[str, Any]) -> dict[str, Any]:
    start_address = _range_host_address(ip_range.get("start_address"))
    end_address = _range_host_address(ip_range.get("end_address"))
    dnsmasq_options = _mapping(ip_range.get("dnsmasq_options"))
    lease_time = _text(dnsmasq_options.get("lease_time"))
    skip_reasons = _dhcp_range_skip_reasons(ip_range, start_address=start_address, end_address=end_address)
    line_parts = ["dhcp-range", start_address, end_address]
    line = ""
    if lease_time:
        line_parts.append(lease_time)
    if not skip_reasons:
        line = f"{line_parts[0]}={','.join(line_parts[1:])}"
    return {
        "desired_ip_range": _text(ip_range.get("name")),
        "desired_ip_range_id": _pk(ip_range),
        "dnsmasq_options": dnsmasq_options,
        "end_address": end_address,
        "generate_dnsmasq": bool(ip_range.get("generate_dnsmasq", False)),
        "lease_time": lease_time,
        "lifecycle": _text(ip_range.get("lifecycle")),
        "line": line,
        "range_policy": _text(ip_range.get("range_policy")),
        "skip_reasons": sorted(set(skip_reasons)),
        "slug": _text(ip_range.get("slug")),
        "start_address": start_address,
    }


def _dhcp_range_skip_reasons(ip_range: Mapping[str, Any], *, start_address: str, end_address: str) -> list[str]:
    reasons = []
    lifecycle = _text(ip_range.get("lifecycle"))
    range_policy = _text(ip_range.get("range_policy"))
    if not bool(ip_range.get("generate_dnsmasq", False)):
        reasons.append("generate_dnsmasq_false")
    if lifecycle not in ELIGIBLE_NODE_LIFECYCLES:
        reasons.append("range_lifecycle_not_exportable")
    if range_policy != "dhcp_dynamic_pool":
        reasons.append("range_policy_not_dhcp_dynamic_pool")
    if not start_address:
        reasons.append("invalid_start_address")
    if not end_address:
        reasons.append("invalid_end_address")
    if start_address and end_address:
        try:
            start_ip = ip_interface(start_address).ip
            end_ip = ip_interface(end_address).ip
            if start_ip.version != end_ip.version:
                reasons.append("address_family_mismatch")
            elif int(start_ip) > int(end_ip):
                reasons.append("range_start_after_end")
        except ValueError:
            reasons.append("invalid_range_address")
    return reasons


def _skip_entry(endpoint: Mapping[str, Any], item_type: str, reasons: list[str]) -> dict[str, Any]:
    desired_node = _mapping(endpoint.get("desired_node"))
    return {
        "desired_endpoint_id": _pk(endpoint),
        "desired_node": _text(desired_node.get("name")),
        "desired_node_id": _pk(desired_node),
        "desired_node_slug": _text(desired_node.get("slug")),
        "dns_name": _text(endpoint.get("dns_name")),
        "endpoint_name": _text(endpoint.get("name")),
        "endpoint_type": _text(endpoint.get("endpoint_type")),
        "ip_policy": _text(endpoint.get("ip_policy")),
        "item_type": item_type,
        "reasons": sorted(set(reasons)),
    }


def _range_skip_entry(ip_range: Mapping[str, Any], reasons: list[str]) -> dict[str, Any]:
    return {
        "desired_ip_range": _text(ip_range.get("name")),
        "desired_ip_range_id": _pk(ip_range),
        "dns_name": "",
        "endpoint_name": "",
        "endpoint_type": "",
        "desired_node_slug": "",
        "item_type": "dhcp_range",
        "range_policy": _text(ip_range.get("range_policy")),
        "reasons": sorted(set(reasons)),
        "slug": _text(ip_range.get("slug")),
        "start_address": _range_host_address(ip_range.get("start_address")),
        "end_address": _range_host_address(ip_range.get("end_address")),
    }


def _evaluation_for(obj: Mapping[str, Any] | None, evaluations: Mapping[str, Any] | None) -> Any | None:
    if not obj or not evaluations:
        return None
    return evaluations.get(_pk(obj))


def _evaluation_data(evaluation: Mapping[str, Any] | None) -> dict[str, Any]:
    if not evaluation:
        return {}
    return {
        **evaluation,
        "actual_refs": evaluation.get("actual_refs") or [],
        "deterministic_summary": evaluation.get("deterministic_summary") or {},
        "observed_facts": evaluation.get("observed_facts") or {},
    }


def _unique_actual_refs(mac_candidates: list[dict[str, Any]], node_actual_refs: list[Any]) -> list[dict[str, Any]]:
    refs = []
    for candidate in mac_candidates:
        ref = candidate.get("actual_node_ref")
        if isinstance(ref, dict):
            refs.append(ref)
    for ref in node_actual_refs:
        if isinstance(ref, dict):
            refs.append(ref)

    unique = {}
    for ref in refs:
        key = (_text(ref.get("object_type")), _text(ref.get("id")), _text(ref.get("name")))
        unique[key] = {
            "object_type": key[0],
            "id": key[1],
            "name": key[2],
        }
    return [unique[key] for key in sorted(unique)]


def _host_address(value: str) -> str:
    try:
        return str(ip_interface(value).ip)
    except ValueError:
        return value.split("/", maxsplit=1)[0]


def _range_host_address(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    try:
        return str(ip_interface(text).ip)
    except ValueError:
        return ""


def _normalize_mac(value: Any) -> str:
    text = re.sub(r"[^0-9A-Fa-f]", "", _text(value))
    if len(text) != 12:
        return ""
    return ":".join(text[index : index + 2].lower() for index in range(0, 12, 2))


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _pk(obj: Mapping[str, Any] | None) -> str:
    if not obj:
        return ""
    return str(obj.get("id") or obj.get("pk") or "")


def _dns_record_sort_key(record: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        record["record_name"],
        record["desired_node_slug"],
        record["endpoint_type"],
        record["endpoint_name"],
    )


def _dhcp_reservation_sort_key(reservation: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        reservation["dns_name"],
        reservation["desired_node_slug"],
        reservation["endpoint_type"],
        reservation["desired_endpoint"],
    )


def _dhcp_range_sort_key(ip_range: dict[str, Any]) -> tuple[str, str, str]:
    return (
        ip_range["start_address"],
        ip_range["end_address"],
        ip_range["slug"],
    )


def _skip_sort_key(entry: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        _text(entry.get("item_type")),
        _text(entry.get("dns_name") or entry.get("start_address")),
        _text(entry.get("desired_node_slug") or entry.get("slug")),
        _text(entry.get("endpoint_type") or entry.get("range_policy")),
        _text(entry.get("endpoint_name") or entry.get("desired_ip_range")),
    )
