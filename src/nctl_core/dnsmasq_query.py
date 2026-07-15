"""Adapts a `SourceSnapshot` into `nctl_core.dnsmasq`'s plain-mapping input
shape (Phase 1 Step 2; Phase 2 Step 4 MAC-source switch).

Originally this module fetched desired endpoints/IP ranges *and*
`intent_evaluations` in one combined GraphQL query. Phase 2 Step 4 replaces
the `intent_evaluations` half with the ported `drift/evaluation.py`
computation over a `SourceSnapshot` (Decision 1 in `p2/plan.md`: the Evaluate
Jobs and `IntentEvaluation` model are deleted in Step 6, so nothing should
depend on reading them back). Evaluations are computed fresh on every render
instead of read from a possibly-stale persisted row — there is exactly one
evaluation per target now, so the old `latest_evaluations` reduction (which
existed to pick the newest of possibly-many persisted rows for the same
target) is gone too.

`export_dnsmasq_records`'s input contract is unchanged: mapping-shaped
endpoints/ip_ranges (with a `desired_node` sub-mapping carrying
`id`/`name`/`slug`/`lifecycle`) and `{target_id: row}` evaluation mappings
shaped like an `IntentEvaluation` GraphQL row
(`observed_facts`/`deterministic_summary`/`actual_refs`). `dnsmasq.py` itself
needed no changes — Parity Gate B in `p2/report4.md` proves byte-identical
`render dnsmasq` output across the switch on live data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nctl_core.drift.evaluation_snapshot import evaluate_all_endpoints, evaluate_all_nodes
from nctl_core.sources.desired import DesiredEndpoint, DesiredIPRange, DesiredNode
from nctl_core.sources.snapshot import SourceSnapshot


@dataclass(frozen=True)
class DnsmasqFetch:
    """Renderer-ready inputs: mappings with lowercased enum fields, one fresh
    evaluation per target."""

    endpoints: list[dict[str, Any]]
    ip_ranges: list[dict[str, Any]]
    endpoint_evaluations: dict[str, dict[str, Any]]
    node_evaluations: dict[str, dict[str, Any]]


def dnsmasq_inputs_from_snapshot(snapshot: SourceSnapshot) -> DnsmasqFetch:
    nodes_by_id = {node.id: node for node in snapshot.desired.nodes}
    node_evaluations = evaluate_all_nodes(snapshot)
    endpoint_evaluations = evaluate_all_endpoints(snapshot, node_evaluations)

    return DnsmasqFetch(
        endpoints=[_endpoint_mapping(endpoint, nodes_by_id.get(endpoint.node_id)) for endpoint in snapshot.desired.endpoints],
        ip_ranges=[_ip_range_mapping(ip_range) for ip_range in snapshot.desired.ip_ranges],
        endpoint_evaluations={target_id: evaluation.as_row() for target_id, evaluation in endpoint_evaluations.items()},
        node_evaluations={target_id: evaluation.as_row() for target_id, evaluation in node_evaluations.items()},
    )


def _endpoint_mapping(endpoint: DesiredEndpoint, node: DesiredNode | None) -> dict[str, Any]:
    return {
        "id": endpoint.id,
        "name": endpoint.name,
        "endpoint_type": endpoint.endpoint_type,
        "ip_address": endpoint.ip_address,
        "ip_policy": endpoint.ip_policy,
        "dns_name": endpoint.dns_name,
        "mdns_name": endpoint.mdns_name,
        "vpn_dns_name": endpoint.vpn_dns_name,
        "generate_dnsmasq": endpoint.generate_dnsmasq,
        "dnsmasq_record_type": endpoint.dnsmasq_record_type,
        "desired_node": _node_mapping(node) if node is not None else {"id": endpoint.node_id, "slug": endpoint.node_slug},
    }


def _node_mapping(node: DesiredNode) -> dict[str, Any]:
    return {"id": node.id, "name": node.name, "slug": node.slug, "lifecycle": node.lifecycle}


def _ip_range_mapping(ip_range: DesiredIPRange) -> dict[str, Any]:
    return {
        "id": ip_range.id,
        "name": ip_range.name,
        "slug": ip_range.slug,
        "start_address": ip_range.start_address,
        "end_address": ip_range.end_address,
        "range_policy": ip_range.range_policy,
        "lifecycle": ip_range.lifecycle,
        "generate_dnsmasq": ip_range.generate_dnsmasq,
        "dnsmasq_options": ip_range.dnsmasq_options,
    }
