"""Read-only SSH trust preflight for reconcile (fix_sshkey Step 5, Design Decision 5).

Presence of a node's alias in the managed known_hosts store is not proof that
the node's *currently reachable* route offers that key -- this module makes
two distinct, read-only checks:

- `check_ssh_enrollment`: does the managed store have at least one entry
  under the node's stable alias at all? Missing entries are
  `ssh_host_key_unenrolled` with the exact `nctl ssh enroll <slug>` remediation.
- `verify_offered_keys`: for already-enrolled hosts, does the mDNS endpoint
  currently offer a key that matches the managed entry? A mismatch or
  unreachable endpoint fails closed (`ssh_host_key_mismatch` /
  `ssh_host_key_unreachable`) before any mutating action runs.

Neither check can authorize a new key -- only `nctl ssh enroll` (ssh_enroll.py)
mutates the managed store. Ledger-only reconcilers (`link_actual_node`,
`reconcile_ipam`) never touch a physical node over SSH, so hosts touched only
by those actions are excluded from `ssh_required_host_slugs` and never gate a
plan on enrollment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from pydantic import BaseModel

from nctl_core.config import Config
from nctl_core.hosts_intent import select_mdns_endpoint
from nctl_core.production.adapter import build_production_node_inputs
from nctl_core.production.composer import resolve_effective_route, try_resolve_operational_values
from nctl_core.production.contract import ContractError
from nctl_core.reconcile.model import ReconcilePlan
from nctl_core.sources.desired import DesiredSnapshot
from nctl_core.sources.snapshot import SourceSnapshot
from nctl_core.ssh_enroll import (
    SshProbeRunner,
    entries_for_lookup_name,
    read_raw_lines,
    scan_offered_keys,
)
from nctl_core.ssh_trust import SshTrustError, derive_host_key_alias, managed_lookup_name

# Only reconcilers that actually connect to the node over SSH require enrollment;
# `link_actual_node` (Nautobot metadata patch) and `reconcile_ipam` (Nautobot Job)
# never do, so ledger-only plans are never blocked by an unrelated unenrolled host.
SSH_REQUIRING_RECONCILER_IDS = frozenset({"observe_node", "service_profile", "dnsmasq_config"})

STATUS_READY = "ready"
STATUS_UNENROLLED = "unenrolled"
STATUS_MISMATCH = "mismatch"
STATUS_UNREACHABLE = "unreachable"


class SshPreflightEntry(BaseModel):
    slug: str
    alias: str = ""
    status: str
    detail: str = ""


@dataclass(frozen=True)
class RouteOverrides:
    """Wraps an explicit slug -> route map for `verify_offered_keys` (fix_sshkey2 Step 3).

    Presence of this wrapper -- even wrapping an empty `routes` dict -- means
    production mode: a slug absent from `routes` is
    `no_resolvable_production_route`, never a silent mDNS fallback. Passing
    `None` (no wrapper at all) instead of an instance of this class means
    bootstrap mode: select the mDNS endpoint per node. A plain
    `dict | None` parameter cannot make this distinction safely, since an
    empty dict and `None` are both falsy and easy to conflate with `or {}`.
    """

    routes: dict[str, str]


def ssh_required_host_slugs(
    plan: ReconcilePlan, *, reconciler_ids: frozenset[str] | None = None
) -> set[str]:
    """Return the node slugs touched by an SSH-requiring action in `plan`.

    `reconciler_ids` narrows which SSH-requiring reconcilers count; the
    default is all of `SSH_REQUIRING_RECONCILER_IDS`. The executor passes
    `{"observe_node"}` alone for the live mDNS scan gate, since that is the
    only phase guaranteed to still use the bootstrap mDNS route -- a
    service-phase host may have production select a different route entirely,
    so scanning it over mDNS could produce a false `unreachable`.
    """
    ids = reconciler_ids if reconciler_ids is not None else SSH_REQUIRING_RECONCILER_IDS
    slugs: set[str] = set()
    for action in plan.actions:
        if action.reconciler_id not in ids:
            continue
        # service_profile/dnsmasq_config actions target the *service* (their
        # `targets` are kind="service"); the node slugs they actually touch
        # live in parameters["host_slugs"] (reconcilers.plan_service_profile).
        # observe_node's targets are the nodes themselves and it sets no
        # host_slugs parameter, so this falls through to the target loop.
        host_slugs = action.parameters.get("host_slugs")
        if host_slugs:
            slugs.update(host_slugs)
            continue
        slugs.update(target.slug for target in action.targets if target.kind == "node" and target.slug)
    return slugs


def _resolve_alias_and_lookup_name(
    snapshot: DesiredSnapshot, slug: str
) -> tuple[str, str, str | None]:
    """Return (alias, lookup_name, error_detail). error_detail is set only on failure.

    `lookup_name` is always the bare managed alias: the managed store's key
    is independent of `ansible_port` (see `ssh_trust.managed_lookup_name`).
    """
    node = next((n for n in snapshot.nodes if n.slug == slug), None)
    if node is None:
        return "", "", "unknown_host"
    try:
        alias = derive_host_key_alias(node.id)
    except SshTrustError as exc:
        return "", "", str(exc)
    return alias, managed_lookup_name(alias), None


def check_ssh_enrollment(
    cfg: Config, host_slugs: Iterable[str], snapshot: DesiredSnapshot
) -> list[SshPreflightEntry]:
    """Read-only: does every host in `host_slugs` have a managed known_hosts entry?

    Presence alone does not prove the current route offers that key -- see
    `verify_offered_keys` for the read-only rejection check that does.
    """
    known_hosts_path = cfg.resolved_ssh_known_hosts_file()
    raw_lines = read_raw_lines(known_hosts_path)
    entries = []
    for slug in sorted(host_slugs):
        alias, lookup_name, error = _resolve_alias_and_lookup_name(snapshot, slug)
        if error is not None:
            entries.append(SshPreflightEntry(slug=slug, status=STATUS_UNENROLLED, detail=error))
            continue
        if entries_for_lookup_name(raw_lines, lookup_name):
            entries.append(SshPreflightEntry(slug=slug, alias=alias, status=STATUS_READY))
        else:
            entries.append(SshPreflightEntry(slug=slug, alias=alias, status=STATUS_UNENROLLED))
    return entries


def resolve_production_routes(
    source_snapshot: SourceSnapshot, host_slugs: Iterable[str], generated_at: str
) -> dict[str, str]:
    """Resolve the `ansible_host` production would currently use for each of `host_slugs`.

    Reuses `production.composer.resolve_effective_route` -- the exact same
    connection-resolution pipeline `nctl render production` uses -- so this
    never becomes a second, potentially disagreeing, route-selection
    implementation. A slug whose route cannot be resolved (ineligible node,
    missing facts, contract error) is simply absent from the returned map;
    callers treat that as unreachable rather than raising.
    """
    wanted = set(host_slugs)
    routes: dict[str, str] = {}
    for node in build_production_node_inputs(source_snapshot):
        if node.slug not in wanted:
            continue
        effective, finding = try_resolve_operational_values(node, generated_at)
        if finding is not None or effective is None:
            continue
        try:
            connection = resolve_effective_route(node, effective)
        except ContractError:
            continue
        host = connection.get("ansible_host")
        if host:
            routes[node.slug] = host
    return routes


def verify_offered_keys(
    cfg: Config,
    host_slugs: Iterable[str],
    snapshot: DesiredSnapshot,
    probe: SshProbeRunner,
    *,
    route_overrides: RouteOverrides | None = None,
) -> list[SshPreflightEntry]:
    """Scan each already-enrolled host's current route and compare against the managed key.

    Without `route_overrides` (`None`), this is bootstrap mode: the mDNS
    endpoint is used. With a `RouteOverrides` instance (from
    `resolve_production_routes`, fed the *same* generation's
    `SourceSnapshot`/`generated_at` -- see `ProductionRenderContext`), this is
    production mode: the production-resolved `ansible_host` is scanned
    instead, and a slug missing from `route_overrides.routes` is
    `no_resolvable_production_route` -- it never falls back to mDNS, which is
    reserved for bootstrap and may not even be what the service-phase host
    answers on. A scan can only prove a mismatch against an already-trusted
    key -- it never authorizes a new one. Unenrolled hosts are reported as
    such rather than scanned.
    """
    known_hosts_path = cfg.resolved_ssh_known_hosts_file()
    raw_lines = read_raw_lines(known_hosts_path)
    entries = []
    for slug in sorted(host_slugs):
        alias, lookup_name, error = _resolve_alias_and_lookup_name(snapshot, slug)
        if error is not None:
            entries.append(SshPreflightEntry(slug=slug, status=STATUS_UNENROLLED, detail=error))
            continue
        managed = entries_for_lookup_name(raw_lines, lookup_name)
        if not managed:
            entries.append(SshPreflightEntry(slug=slug, alias=alias, status=STATUS_UNENROLLED))
            continue

        node = next((n for n in snapshot.nodes if n.slug == slug), None)
        if route_overrides is not None:
            route = route_overrides.routes.get(slug)
            if not route:
                entries.append(
                    SshPreflightEntry(
                        slug=slug, alias=alias, status=STATUS_UNREACHABLE, detail="no_resolvable_production_route"
                    )
                )
                continue
        else:
            endpoints = [e for e in snapshot.endpoints if e.node_id == node.id]
            endpoint = select_mdns_endpoint(endpoints)
            route = endpoint.mdns_name if endpoint else None
        if not route:
            entries.append(
                SshPreflightEntry(slug=slug, alias=alias, status=STATUS_UNREACHABLE, detail="no_resolvable_route")
            )
            continue
        override = next((o for o in snapshot.operational_overrides if o.node_id == node.id), None)
        port = override.ansible_port if override and override.ansible_port else 22
        try:
            offered = scan_offered_keys(probe, route, port, cfg.ssh.keyscan_timeout_seconds)
        except SshTrustError as exc:
            entries.append(SshPreflightEntry(slug=slug, alias=alias, status=STATUS_UNREACHABLE, detail=str(exc)))
            continue

        managed_pairs = {(e.key_type, e.key_blob_b64) for e in managed}
        offered_pairs = {(k.key_type, k.key_blob_b64) for k in offered}
        if managed_pairs & offered_pairs:
            entries.append(SshPreflightEntry(slug=slug, alias=alias, status=STATUS_READY))
        else:
            entries.append(SshPreflightEntry(slug=slug, alias=alias, status=STATUS_MISMATCH))
    return entries
