"""GraphQL fetch layer for the dnsmasq renderer (Phase 1 Step 2).

Fetches desired endpoints, IP ranges, and intent evaluations in a single request
and adapts the result into the plain-mapping shape `nctl_core.dnsmasq` expects.

Two things the live schema does that the ORM-based renderer never had to deal
with:

- Nautobot's GraphQL layer serializes ChoiceField values (`endpoint_type`,
  `ip_policy`, `dnsmasq_record_type`, `lifecycle`, `range_policy`) as their
  UPPERCASE enum *name*, not the lowercase value stored in the database and
  used throughout `dnsmasq.py`'s vocabulary (`"primary"`, `"dhcp_reserved"`,
  ...). This module lowercases them back on the way in. Free-form JSON fields
  (`observed_facts`, `actual_refs`, ...) are untouched — they're `GenericScalar`,
  not choice fields, and already round-trip in the DB's native case.
- `intent_evaluations` accepts a `target_type` filter (confirmed against the
  live schema: lowercase `"desired_endpoint"` / `"desired_node"` both filter
  correctly despite the field echoing back as uppercase), so both evaluation
  sets are fetched pre-split via query aliases instead of one fetch-and-split
  client-side pass.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from nctl_core.nautobot import NautobotClient

ENDPOINT_TARGET_TYPE = "desired_endpoint"
NODE_TARGET_TYPE = "desired_node"

DNSMASQ_QUERY = """
{
  desired_endpoints {
    id
    name
    endpoint_type
    ip_address
    ip_policy
    dns_name
    mdns_name
    vpn_dns_name
    generate_dnsmasq
    dnsmasq_record_type
    desired_node {
      id
      name
      slug
      lifecycle
    }
  }
  desired_ip_ranges {
    id
    name
    slug
    start_address
    end_address
    range_policy
    lifecycle
    generate_dnsmasq
    dnsmasq_options
  }
  endpoint_evaluations: intent_evaluations(target_type: "desired_endpoint") {
    target_id
    reviewed_at
    created
    observed_facts
    deterministic_summary
    actual_refs
  }
  node_evaluations: intent_evaluations(target_type: "desired_node") {
    target_id
    reviewed_at
    created
    observed_facts
    deterministic_summary
    actual_refs
  }
}
"""


@dataclass(frozen=True)
class DnsmasqFetch:
    """Renderer-ready inputs: mappings with lowercased enum fields, evaluations
    reduced to one row per target."""

    endpoints: list[dict[str, Any]]
    ip_ranges: list[dict[str, Any]]
    endpoint_evaluations: dict[str, dict[str, Any]]
    node_evaluations: dict[str, dict[str, Any]]


def fetch_dnsmasq_inputs(client: NautobotClient) -> DnsmasqFetch:
    data = client.graphql(DNSMASQ_QUERY)
    return DnsmasqFetch(
        endpoints=[_normalize_endpoint(row) for row in data["desired_endpoints"]],
        ip_ranges=[_normalize_ip_range(row) for row in data["desired_ip_ranges"]],
        endpoint_evaluations=latest_evaluations(data["endpoint_evaluations"]),
        node_evaluations=latest_evaluations(data["node_evaluations"]),
    )


def latest_evaluations(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Client-side equivalent of nintent's `_latest_evaluations`: group by
    `target_id`, keep one row per target ordered by `-reviewed_at, -created`.

    Ties (equal `reviewed_at` and `created`, or both null) fall back to input
    order, same as a stable queryset iteration would for equal sort keys.
    """
    by_target: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_target[str(row["target_id"])].append(row)

    latest: dict[str, dict[str, Any]] = {}
    for target_id, group in by_target.items():
        ranked = sorted(
            enumerate(group),
            key=lambda item: (item[1].get("reviewed_at") or "", item[1].get("created") or "", -item[0]),
            reverse=True,
        )
        latest[target_id] = ranked[0][1]
    return latest


def _normalize_endpoint(raw: dict[str, Any]) -> dict[str, Any]:
    endpoint = dict(raw)
    endpoint["endpoint_type"] = _lower(endpoint.get("endpoint_type"))
    endpoint["ip_policy"] = _lower(endpoint.get("ip_policy"))
    endpoint["dnsmasq_record_type"] = _lower(endpoint.get("dnsmasq_record_type"))
    node = endpoint.get("desired_node")
    if isinstance(node, dict):
        endpoint["desired_node"] = {**node, "lifecycle": _lower(node.get("lifecycle"))}
    return endpoint


def _normalize_ip_range(raw: dict[str, Any]) -> dict[str, Any]:
    ip_range = dict(raw)
    ip_range["range_policy"] = _lower(ip_range.get("range_policy"))
    ip_range["lifecycle"] = _lower(ip_range.get("lifecycle"))
    return ip_range


def _lower(value: Any) -> Any:
    return value.lower() if isinstance(value, str) else value
