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

from typing import Iterable, Mapping

from pydantic import BaseModel, Field

from nctl_core.config import Config
from nctl_core.hosts_intent import select_mdns_endpoint
from nctl_core.production.composer import ResolvedSshTarget
from nctl_core.reconcile.model import ReconcileAction, ReconcilePlan
from nctl_core.sources.desired import DesiredSnapshot
from nctl_core.ssh_enroll import (
    SshProbeRunner,
    load_managed_ssh_store,
    scan_offered_keys,
)
from nctl_core.ssh_trust import (
    SshTrustError,
    compute_sha256_fingerprint,
    derive_host_key_alias,
    managed_lookup_name,
)

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
    # fix_sshkey3 Step 2 (contract item 7): a richer public preflight record.
    # `phase` distinguishes which gate produced this entry (`enrollment`,
    # `bootstrap_route`, `production_route`); route/port/generation_id/round
    # and the SHA-256 fingerprints let an operation artifact prove exactly
    # what was checked without ever including a raw key blob. Left at their
    # defaults for call sites (enrollment presence, bootstrap mDNS) that
    # predate this contract and have nothing meaningful to report for them.
    phase: str = ""
    round: int | None = None
    route: str = ""
    port: int | None = None
    generation_id: str = ""
    managed_fingerprints: list[str] = Field(default_factory=list)
    offered_fingerprints: list[str] = Field(default_factory=list)


def action_host_slugs(action: ReconcileAction) -> set[str]:
    """Return the node slugs one reconcile action actually touches.

    `service_profile`/`dnsmasq_config` actions target the *service* (their
    `targets` are kind="service"); the node slugs they actually touch live in
    `parameters["host_slugs"]` (`reconcilers.plan_service_profile`).
    `observe_node`/ledger actions target the nodes themselves and set no
    `host_slugs` parameter, so this falls through to the target loop. Shared
    by `ssh_required_host_slugs` (SSH gating) and the executor's
    post-actuation observation host list (fix_sshkey3 Step 2 item 8) so the
    two can never disagree on which node a service action's evidence belongs to.
    """
    host_slugs = action.parameters.get("host_slugs")
    if host_slugs:
        return set(host_slugs)
    return {target.slug for target in action.targets if target.kind == "node" and target.slug}


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
        slugs.update(action_host_slugs(action))
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
    store = load_managed_ssh_store(known_hosts_path)
    entries = []
    for slug in sorted(host_slugs):
        alias, lookup_name, error = _resolve_alias_and_lookup_name(snapshot, slug)
        if error is not None:
            entries.append(SshPreflightEntry(slug=slug, status=STATUS_UNENROLLED, detail=error))
            continue
        if store.entries_for(lookup_name):
            entries.append(SshPreflightEntry(slug=slug, alias=alias, status=STATUS_READY))
        else:
            entries.append(SshPreflightEntry(slug=slug, alias=alias, status=STATUS_UNENROLLED))
    return entries


def verify_offered_keys(
    cfg: Config,
    host_slugs: Iterable[str],
    snapshot: DesiredSnapshot,
    probe: SshProbeRunner,
) -> list[SshPreflightEntry]:
    """Bootstrap-only: scan each already-enrolled host's mDNS endpoint and compare against the managed key.

    fix_sshkey3 Step 2: production scanning no longer goes through this
    function at all -- it used to take an optional `route_overrides`
    (`RouteOverrides`/`resolve_production_routes`) that re-resolved a route
    from a possibly-stale `SourceSnapshot`, decoupled from the port/identity
    that snapshot actually used. `verify_resolved_ssh_targets` (below) is the
    one production-mode scan now, fed `ResolvedSshTarget`s built by the exact
    composition run being verified. This function keeps doing only what
    bootstrap ever needed: the mDNS endpoint, port 22 unless a desired
    operational override says otherwise. A scan can only prove a mismatch
    against an already-trusted key -- it never authorizes a new one.
    Unenrolled hosts are reported as such rather than scanned.
    """
    known_hosts_path = cfg.resolved_ssh_known_hosts_file()
    store = load_managed_ssh_store(known_hosts_path)
    entries = []
    for slug in sorted(host_slugs):
        alias, lookup_name, error = _resolve_alias_and_lookup_name(snapshot, slug)
        if error is not None:
            entries.append(SshPreflightEntry(slug=slug, status=STATUS_UNENROLLED, detail=error, phase="bootstrap_route"))
            continue
        managed = store.entries_for(lookup_name)
        if not managed:
            entries.append(SshPreflightEntry(slug=slug, alias=alias, status=STATUS_UNENROLLED, phase="bootstrap_route"))
            continue

        node = next((n for n in snapshot.nodes if n.slug == slug), None)
        endpoints = [e for e in snapshot.endpoints if e.node_id == node.id]
        endpoint = select_mdns_endpoint(endpoints)
        route = endpoint.mdns_name if endpoint else None
        if not route:
            entries.append(
                SshPreflightEntry(
                    slug=slug, alias=alias, status=STATUS_UNREACHABLE, detail="no_resolvable_route",
                    phase="bootstrap_route",
                )
            )
            continue
        override = next((o for o in snapshot.operational_overrides if o.node_id == node.id), None)
        port = override.ansible_port if override and override.ansible_port else 22
        try:
            offered = scan_offered_keys(probe, route, port, cfg.ssh.keyscan_timeout_seconds)
        except SshTrustError as exc:
            entries.append(
                SshPreflightEntry(
                    slug=slug, alias=alias, status=STATUS_UNREACHABLE, detail=str(exc), phase="bootstrap_route"
                )
            )
            continue

        managed_pairs = {(e.key_type, e.key_blob_b64) for e in managed}
        offered_pairs = {(k.key_type, k.key_blob_b64) for k in offered}
        status = STATUS_READY if managed_pairs & offered_pairs else STATUS_MISMATCH
        entries.append(
            SshPreflightEntry(slug=slug, alias=alias, status=status, route=route, port=port, phase="bootstrap_route")
        )
    return entries


def verify_resolved_ssh_targets(
    cfg: Config,
    host_slugs: Iterable[str],
    ssh_targets: Mapping[str, ResolvedSshTarget],
    probe: SshProbeRunner,
    *,
    round_index: int | None = None,
) -> list[SshPreflightEntry]:
    """Production-mode scan: verify each target's *own* generation-exact alias/route/port.

    fix_sshkey3 Step 2 (Corrected contract 5): `ssh_targets` must be the
    `ProductionRenderContext.ssh_targets` map produced by the exact
    composition run this round just installed -- never a separately
    re-resolved snapshot. A slug missing from the map (a planned service host
    that composition did not actually include in `ssh_hosts`) is
    `no_resolvable_production_target`; it never falls back to mDNS and never
    substitutes any other route. A scan can only prove a mismatch against an
    already-trusted key -- it never authorizes a new one.
    """
    known_hosts_path = cfg.resolved_ssh_known_hosts_file()
    store = load_managed_ssh_store(known_hosts_path)
    entries = []
    for slug in sorted(host_slugs):
        target = ssh_targets.get(slug)
        if target is None:
            entries.append(
                SshPreflightEntry(
                    slug=slug, status=STATUS_UNREACHABLE, detail="no_resolvable_production_target",
                    phase="production_route", round=round_index,
                )
            )
            continue

        lookup_name = managed_lookup_name(target.alias)
        managed = store.entries_for(lookup_name)
        managed_fingerprints = sorted({compute_sha256_fingerprint(e.key_blob_b64) for e in managed})
        if not managed:
            entries.append(
                SshPreflightEntry(
                    slug=slug, alias=target.alias, status=STATUS_UNENROLLED, phase="production_route",
                    round=round_index, route=target.route, port=target.port, generation_id=target.generation_id,
                )
            )
            continue

        try:
            offered = scan_offered_keys(probe, target.route, target.port, cfg.ssh.keyscan_timeout_seconds)
        except SshTrustError as exc:
            entries.append(
                SshPreflightEntry(
                    slug=slug, alias=target.alias, status=STATUS_UNREACHABLE, detail=str(exc),
                    phase="production_route", round=round_index, route=target.route, port=target.port,
                    generation_id=target.generation_id, managed_fingerprints=managed_fingerprints,
                )
            )
            continue

        managed_pairs = {(e.key_type, e.key_blob_b64) for e in managed}
        offered_pairs = {(k.key_type, k.key_blob_b64) for k in offered}
        offered_fingerprints = sorted({compute_sha256_fingerprint(k.key_blob_b64) for k in offered})
        status = STATUS_READY if managed_pairs & offered_pairs else STATUS_MISMATCH
        entries.append(
            SshPreflightEntry(
                slug=slug, alias=target.alias, status=status, phase="production_route", round=round_index,
                route=target.route, port=target.port, generation_id=target.generation_id,
                managed_fingerprints=managed_fingerprints, offered_fingerprints=offered_fingerprints,
            )
        )
    return entries
