"""Deterministic Ansible bootstrap inventory export for desired nodes (Phase 1.5 Step 1).

Ported from nintent's ``nautobot_intent_catalog/ansible_inventory.py``
(schema 2.0) with the input shape adapted from ORM objects to the Phase 2
desired-snapshot models (`sources.desired.DesiredNode` / `DesiredEndpoint`):
the caller passes the flat node and endpoint lists and this module groups
endpoints by ``node_id``, replacing the ORM's ``prefetch_related``. Everything
observable — eligibility gates, mDNS endpoint selection order, sort keys, the
``skip_reasons`` vocabulary, group/hostvars structure, YAML/JSON serialization
options — is unchanged. Schema bumped to 3.0 because the generator changed
(nothing asserts on it; the header stays honest for humans and later phases).

Schema bumped to 4.0 (basic_service plan Step 2): active `DesiredServicePlacement`
rows are now folded in, emitting one bare (host-var-free) group per
`deployment_profile` alongside `ssh_hosts` -- the same "groups derive from
placements" rule `production/composer.py` already applies, so bootstrap-time
playbooks can target a service group (e.g. `dnsmasq_server`) over mDNS before
any production inventory exists. A placement whose profile is unknown or
whose node was not exported to `ssh_hosts` is reported in `skipped`
(`item_type: desired_service_placement`) rather than silently dropped.

Schema bumped to 5.0 (fix_sshkey Step 3): every `ssh_hosts` member also
carries `nctl_ssh_host_key_alias` and `ansible_ssh_common_args`, derived only
from the immutable DesiredNode UUID (see
`devdocs/small/fix_sshkey/plan.md`), when the caller supplies
`ssh_known_hosts_file`. This makes bootstrap SSH trust follow the node's
identity across mDNS/`.home.arpa`/IP/Tailscale routes instead of the
currently selected endpoint spelling.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable

import yaml

from nctl_core.sources.desired import DesiredEndpoint, DesiredNode, DesiredServicePlacement
from nctl_core.ssh_trust import build_ansible_ssh_common_args, derive_host_key_alias

HOSTS_INTENT_SCHEMA_VERSION = "5.0"
ELIGIBLE_NODE_LIFECYCLES = frozenset({"planned", "approved", "active"})
# service_host represents a host whose eventual Nautobot object may be either a
# device or a virtual machine, so it is eligible for bootstrap discovery too.
ELIGIBLE_NODE_TYPES = frozenset({"device", "virtual_machine", "service_host"})


@dataclass(frozen=True)
class HostsIntentExport:
    """Serializable hosts intent export payload."""

    summary: dict[str, Any]
    inventory: dict[str, Any]
    hosts: list[dict[str, Any]]
    skipped: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "inventory": self.inventory,
            "hosts": self.hosts,
            "skipped": self.skipped,
        }


def export_hosts_intent(
    nodes: Iterable[DesiredNode],
    endpoints: Iterable[DesiredEndpoint],
    *,
    placements: Iterable[DesiredServicePlacement] = (),
    profile_groups: dict[str, str] | None = None,
    include_skipped: bool = True,
    ssh_known_hosts_file: str | None = None,
) -> HostsIntentExport:
    """Return a deterministic minimal Ansible inventory from desired nodes.

    ``ssh_known_hosts_file`` is the resolved managed known_hosts path
    (``cfg.ssh.resolved_known_hosts_file()``); when given, every eligible
    ``ssh_hosts`` member gets ``nctl_ssh_host_key_alias`` and
    ``ansible_ssh_common_args`` derived from its DesiredNode UUID. Real
    callers (`hosts_intent_render.py`, `observation.py`) must always supply
    it; omitting it is only for tests unconcerned with the SSH trust vars.

    ``profile_groups`` maps ``deployment_profile`` name to Ansible group name
    (the same mapping ``production/composer.py`` derives from
    ``deployment_profiles.yml``); active placements whose profile is not in
    this map, or whose node was not exported to ``ssh_hosts``, are reported
    in ``skipped`` instead of silently dropped.
    """

    endpoints_by_node: dict[str, list[DesiredEndpoint]] = {}
    for endpoint in endpoints:
        endpoints_by_node.setdefault(endpoint.node_id, []).append(endpoint)

    nodes_list = list(nodes)
    node_by_id = {node.id: node for node in nodes_list}

    hosts: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    group_members: dict[str, set[str]] = {"ssh_hosts": set()}
    exported_hostname_by_node_id: dict[str, str] = {}
    total_nodes = 0
    exported_nodes = 0
    skipped_nodes = 0

    for node in sorted(nodes_list, key=_node_sort_key):
        total_nodes += 1
        node_skip_reasons = _node_skip_reasons(node)
        endpoint = select_mdns_endpoint(endpoints_by_node.get(node.id, []))
        if endpoint is None:
            node_skip_reasons.append("missing_mdns_name")

        inventory_hostname = _text(node.slug)
        if not _valid_inventory_hostname(inventory_hostname):
            node_skip_reasons.append("invalid_inventory_hostname")

        if node_skip_reasons:
            skipped_nodes += 1
            if include_skipped:
                skipped.append(_node_skip_entry(node, endpoint, node_skip_reasons))
            continue

        exported_nodes += 1
        host_vars = _host_vars(node, endpoint, ssh_known_hosts_file)
        hosts.append(
            {
                "inventory_hostname": inventory_hostname,
                "desired_node": _text(node.name),
                "desired_node_id": _text(node.id),
                "desired_node_slug": _text(node.slug),
                "desired_endpoint": _text(endpoint.name),
                "desired_endpoint_id": _text(endpoint.id),
                "mdns_hostname": _text(endpoint.mdns_name),
                "host_vars": host_vars,
            }
        )
        group_members["ssh_hosts"].add(inventory_hostname)
        exported_hostname_by_node_id[node.id] = inventory_hostname

    profile_groups = profile_groups or {}
    for placement in placements:
        if placement.desired_state != "active":
            continue

        group = profile_groups.get(placement.deployment_profile)
        hostname = exported_hostname_by_node_id.get(placement.node_id)
        placement_skip_reasons = []
        if group is None:
            placement_skip_reasons.append("unknown_deployment_profile")
        if hostname is None:
            placement_skip_reasons.append("node_not_exported")

        if placement_skip_reasons:
            if include_skipped:
                skipped.append(
                    _placement_skip_entry(placement, node_by_id.get(placement.node_id), placement_skip_reasons)
                )
            continue

        group_members.setdefault(group, set()).add(hostname)

    hosts.sort(key=lambda item: item["inventory_hostname"])
    skipped.sort(key=_skip_sort_key)
    inventory = _inventory(hosts, group_members)
    summary = {
        "schema_version": HOSTS_INTENT_SCHEMA_VERSION,
        "total_nodes": total_nodes,
        "exported_hosts": len(hosts),
        "exported_nodes": exported_nodes,
        "skipped_nodes": skipped_nodes,
        "skipped_details": len(skipped),
        "groups": sorted(group for group, members in group_members.items() if members),
    }
    return HostsIntentExport(summary=summary, inventory=inventory, hosts=hosts, skipped=skipped)


def hosts_intent_payload(export: HostsIntentExport, *, generated_at: str) -> dict[str, Any]:
    """Return a stable, machine-readable hosts intent export payload."""

    return {
        "schema_version": HOSTS_INTENT_SCHEMA_VERSION,
        "generated_at": generated_at,
        "summary": export.summary,
        "inventory": export.inventory,
        "hosts": export.hosts,
        "skipped": export.skipped,
    }


def render_hosts_intent_yml(export: HostsIntentExport, *, generated_at: str) -> str:
    """Return an Ansible YAML inventory for bootstrap collection."""

    lines = [
        "# Generated by nctl",
        f"# schema_version: {HOSTS_INTENT_SCHEMA_VERSION}",
        f"# generated_at: {generated_at}",
    ]
    lines.append(yaml.safe_dump(export.inventory, sort_keys=False, default_flow_style=False).rstrip())
    return "\n".join(lines) + "\n"


def render_hosts_intent_json(export: HostsIntentExport, *, generated_at: str) -> str:
    """Return a deterministic JSON representation of a hosts intent export."""

    return json.dumps(
        hosts_intent_payload(export, generated_at=generated_at),
        sort_keys=True,
        ensure_ascii=True,
        indent=2,
    ) + "\n"


def _node_skip_reasons(node: DesiredNode) -> list[str]:
    reasons = []
    if _text(node.lifecycle) not in ELIGIBLE_NODE_LIFECYCLES:
        reasons.append("node_lifecycle_not_exportable")
    if _text(node.node_type) not in ELIGIBLE_NODE_TYPES:
        reasons.append("node_type_not_exportable")
    return reasons


def select_mdns_endpoint(endpoints: list[DesiredEndpoint]) -> DesiredEndpoint | None:
    candidates = [endpoint for endpoint in endpoints if _text(endpoint.mdns_name)]
    if not candidates:
        return None

    for endpoint_type in ("primary", "management"):
        matching = [
            endpoint for endpoint in candidates if _text(endpoint.endpoint_type) == endpoint_type
        ]
        if matching:
            return sorted(matching, key=_endpoint_sort_key)[0]

    return sorted(candidates, key=_endpoint_sort_key)[0]


def _host_vars(node: DesiredNode, endpoint: DesiredEndpoint, ssh_known_hosts_file: str | None) -> dict[str, Any]:
    host_vars = {
        "ansible_host": _text(endpoint.mdns_name),
        "mdns_hostname": _text(endpoint.mdns_name),
        "nintent_inventory_stage": "reserved_name",
        "nintent_desired_node": _text(node.name),
        "nintent_desired_node_slug": _text(node.slug),
        "nintent_desired_node_id": _text(node.id),
        "nintent_desired_endpoint": _text(endpoint.name),
        "nintent_desired_endpoint_id": _text(endpoint.id),
        "name_reserved_only": True,
    }
    if ssh_known_hosts_file is not None:
        alias = derive_host_key_alias(node.id)
        host_vars["nctl_ssh_host_key_alias"] = alias
        host_vars["ansible_ssh_common_args"] = build_ansible_ssh_common_args(alias, ssh_known_hosts_file)
    return host_vars


def _inventory(hosts: list[dict[str, Any]], group_members: dict[str, set[str]]) -> dict[str, Any]:
    host_vars_by_name = {host["inventory_hostname"]: host["host_vars"] for host in hosts}
    children = {}
    for group in sorted(group_members):
        members = sorted(group_members[group])
        if not members:
            continue
        group_hosts = {}
        for member in members:
            group_hosts[member] = host_vars_by_name[member] if group == "ssh_hosts" else {}
        children[group] = {"hosts": group_hosts}
    return {"all": {"children": children}}


def _node_skip_entry(
    node: DesiredNode, endpoint: DesiredEndpoint | None, reasons: list[str]
) -> dict[str, Any]:
    return {
        "item_type": "desired_node",
        "desired_node": _text(node.name),
        "desired_node_id": _text(node.id),
        "desired_node_slug": _text(node.slug),
        "desired_endpoint": _text(endpoint.name) if endpoint else "",
        "desired_endpoint_id": _text(endpoint.id) if endpoint else "",
        "reasons": sorted(set(reasons)),
    }


def _placement_skip_entry(
    placement: DesiredServicePlacement, node: DesiredNode | None, reasons: list[str]
) -> dict[str, Any]:
    return {
        "item_type": "desired_service_placement",
        "desired_node": _text(node.name) if node else "",
        "desired_node_id": _text(placement.node_id),
        "desired_node_slug": _text(node.slug) if node else "",
        "group": _text(placement.deployment_profile),
        "instance_name": _text(placement.instance_name),
        "reasons": sorted(set(reasons)),
    }


def _valid_inventory_hostname(value: str) -> bool:
    return bool(value) and not bool(re.search(r"[\s:]", value))


def _node_sort_key(node: DesiredNode) -> tuple[str, str]:
    return (_text(node.slug), _text(node.name))


def _endpoint_sort_key(endpoint: DesiredEndpoint) -> tuple[str, str]:
    return (_text(endpoint.endpoint_type), _text(endpoint.name))


def _skip_sort_key(entry: dict[str, Any]) -> tuple[str, str, str]:
    return (
        _text(entry.get("item_type")),
        _text(entry.get("desired_node_slug")),
        _text(entry.get("group") or entry.get("desired_endpoint")),
    )


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()
