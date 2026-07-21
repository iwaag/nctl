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

from typing import Iterable

from pydantic import BaseModel

from nctl_core.config import Config
from nctl_core.hosts_intent import select_mdns_endpoint
from nctl_core.reconcile.model import ReconcilePlan
from nctl_core.sources.desired import DesiredSnapshot
from nctl_core.ssh_enroll import (
    SshProbeRunner,
    entries_for_lookup_name,
    read_raw_lines,
    scan_offered_keys,
)
from nctl_core.ssh_trust import SshTrustError, derive_host_key_alias, derive_lookup_name

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
    return {
        target.slug
        for action in plan.actions
        if action.reconciler_id in ids
        for target in action.targets
        if target.kind == "node" and target.slug
    }


def _resolve_alias_and_lookup_name(
    snapshot: DesiredSnapshot, slug: str
) -> tuple[str, str, str | None]:
    """Return (alias, lookup_name, error_detail). error_detail is set only on failure."""
    node = next((n for n in snapshot.nodes if n.slug == slug), None)
    if node is None:
        return "", "", "unknown_host"
    try:
        alias = derive_host_key_alias(node.id)
    except SshTrustError as exc:
        return "", "", str(exc)
    override = next((o for o in snapshot.operational_overrides if o.node_id == node.id), None)
    port = override.ansible_port if override and override.ansible_port else 22
    lookup_name = derive_lookup_name(alias, port)
    return alias, lookup_name, None


def check_ssh_enrollment(
    cfg: Config, host_slugs: Iterable[str], snapshot: DesiredSnapshot
) -> list[SshPreflightEntry]:
    """Read-only: does every host in `host_slugs` have a managed known_hosts entry?

    Presence alone does not prove the current route offers that key -- see
    `verify_offered_keys` for the read-only rejection check that does.
    """
    known_hosts_path = cfg.ssh.resolved_known_hosts_file()
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


def verify_offered_keys(
    cfg: Config,
    host_slugs: Iterable[str],
    snapshot: DesiredSnapshot,
    probe: SshProbeRunner,
) -> list[SshPreflightEntry]:
    """Scan each already-enrolled host's mDNS endpoint and compare against the managed key.

    A scan can only prove a mismatch against an already-trusted key -- it never
    authorizes a new one. Unenrolled hosts are reported as such rather than scanned.
    """
    known_hosts_path = cfg.ssh.resolved_known_hosts_file()
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
        endpoints = [e for e in snapshot.endpoints if e.node_id == node.id]
        endpoint = select_mdns_endpoint(endpoints)
        if endpoint is None or not endpoint.mdns_name:
            entries.append(
                SshPreflightEntry(slug=slug, alias=alias, status=STATUS_UNREACHABLE, detail="no_mdns_endpoint")
            )
            continue
        override = next((o for o in snapshot.operational_overrides if o.node_id == node.id), None)
        port = override.ansible_port if override and override.ansible_port else 22
        try:
            offered = scan_offered_keys(probe, endpoint.mdns_name, port, cfg.ssh.keyscan_timeout_seconds)
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
