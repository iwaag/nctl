"""Structured SSH trust-contract validation for an already-loaded inventory (fix_sshkey2 Step 4).

`nctl apply dnsmasq` actuates against a rendered inventory (`ansible-inventory
--list` output), not a live `SourceSnapshot` -- there is no Nautobot fetch on
this path. This module therefore re-derives the expected trust variables from
each host's own `nintent_desired_node_id` and validates the *rendered* host
vars exactly, instead of trusting a hand-written or stale inventory's
self-reported alias. It shares `production.contract.select_local_route` with
the production composer (Step 3's pure route helper) so route resolution can
never independently drift between composition and this preflight.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nctl_core.production.contract import select_local_route
from nctl_core.reconcile.ssh_preflight import (
    STATUS_MISMATCH,
    STATUS_READY,
    STATUS_UNENROLLED,
    STATUS_UNREACHABLE,
    SshPreflightEntry,
)
from nctl_core.ssh_enroll import SshProbeRunner, entries_for_lookup_name, read_raw_lines, scan_offered_keys
from nctl_core.ssh_trust import (
    SshTrustError,
    build_ansible_ssh_common_args,
    derive_host_key_alias,
    managed_lookup_name,
)


class InventoryTrustError(Exception):
    """One host's rendered SSH trust variables fail the closed contract."""

    def __init__(self, hostname: str, code: str, message: str) -> None:
        self.hostname = hostname
        self.code = code
        super().__init__(message)


# fix_sshkey3 Step 1 (contract item 6): variables that can replace or precede
# the generated `ansible_ssh_common_args` in Ansible's/OpenSSH's option
# ordering. `ansible_ssh_args` in particular is placed *before*
# `ansible_ssh_common_args` by Ansible, and OpenSSH keeps the first value it
# sees for a given `-o` option -- so an inventory carrying both variables can
# make the real connection silently use the attacker-supplied policy while
# `ansible_ssh_common_args` still reads as the correct, closed value. This is
# a closed denylist of every documented Ansible SSH connection variable
# capable of that, checked in addition to (not instead of) the exact
# `ansible_ssh_common_args` equality check above.
FORBIDDEN_INVENTORY_SSH_VARS: tuple[str, ...] = (
    "ansible_ssh_args",
    "ansible_ssh_extra_args",
    "ansible_scp_extra_args",
    "ansible_sftp_extra_args",
    "ansible_ssh_executable",
    "ansible_host_key_checking",
    "ansible_ssh_host_key_checking",
)


def validate_inventory_trust_contract(
    host_vars: dict[str, Any], hostname: str, known_hosts_path: Path
) -> InventoryTrustError | None:
    """Recompute the expected alias/`ansible_ssh_common_args` from the UUID and require exact equality.

    Rejects old-schema, hand-written, or partial trust variables, a
    different known_hosts path, and an incorrect alias -- a non-empty alias
    that merely differs from the UUID-derived value is not enough to pass.
    No unmanaged SSH argument can subsequently weaken the host-key policy,
    since the *entire* `ansible_ssh_common_args` string must match the one
    `build_ansible_ssh_common_args` would generate, not just contain the
    right options. Also closes the allowlist gaps fix_sshkey3 exposed: a
    forbidden connection-policy variable, a non-integer/out-of-range
    `ansible_port`, and a non-`ssh` `ansible_connection` are all rejected
    here, before any managed-file read or network access happens for this
    host (`check_inventory_ssh_preflight` / `build_dnsmasq_apply` always run
    this check first).
    """
    node_id = host_vars.get("nintent_desired_node_id")
    if not isinstance(node_id, str) or not node_id:
        return InventoryTrustError(
            hostname, "missing_desired_node_id", f"{hostname}: missing nintent_desired_node_id"
        )
    try:
        expected_alias = derive_host_key_alias(node_id)
    except SshTrustError as exc:
        return InventoryTrustError(hostname, "invalid_desired_node_id", f"{hostname}: {exc}")

    alias = host_vars.get("nctl_ssh_host_key_alias")
    if alias != expected_alias:
        return InventoryTrustError(
            hostname,
            "ssh_host_key_alias_mismatch",
            f"{hostname}: nctl_ssh_host_key_alias {alias!r} does not match the UUID-derived alias {expected_alias!r}",
        )

    expected_args = build_ansible_ssh_common_args(expected_alias, str(known_hosts_path))
    actual_args = host_vars.get("ansible_ssh_common_args")
    if actual_args != expected_args:
        return InventoryTrustError(
            hostname,
            "ansible_ssh_common_args_mismatch",
            f"{hostname}: ansible_ssh_common_args does not match the closed, controller-generated policy",
        )

    present_forbidden = sorted(var for var in FORBIDDEN_INVENTORY_SSH_VARS if var in host_vars)
    if present_forbidden:
        return InventoryTrustError(
            hostname,
            "ssh_policy_override_rejected",
            f"{hostname}: inventory variable(s) can replace or precede the closed host-key policy: "
            + ", ".join(present_forbidden),
        )

    port = host_vars.get("ansible_port")
    if port is not None and (isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535)):
        return InventoryTrustError(
            hostname,
            "ansible_port_invalid",
            f"{hostname}: ansible_port must be an integer in 1..65535, got {port!r}",
        )

    connection = host_vars.get("ansible_connection")
    if connection is not None and connection != "ssh":
        return InventoryTrustError(
            hostname,
            "ansible_connection_invalid",
            f"{hostname}: ansible_connection must be absent or exactly 'ssh', got {connection!r}",
        )
    return None


def resolve_route_from_host_vars(host_vars: dict[str, Any], hostname: str) -> str | None:
    """Resolve the connection route for `hostname` from its own rendered host vars.

    A bootstrap inventory (`hosts_intent.yml`) exports `ansible_host`
    directly -- used verbatim. A production inventory never exports
    `ansible_host` per host (the composer pops it: it is resolved from a
    Jinja template in `group_vars/all.yml`, not per host) but `ansible-
    inventory --host`/`--list` still reports one for every host, inherited
    unrendered from that group var -- confirmed live: it comes back as the
    literal string `"{{ tailscale_ip | default(local_connection_host, ...)
    }}"`, not a resolved address, since `ansible-inventory` does not
    template variables. Treating that string as a literal target caused a
    real `ssh-keyscan ... getaddrinfo {{:` failure the first time this ran
    against live data. Only a per-host `ansible_host` free of `{{` is used
    verbatim; anything else falls through to the same
    `connection_path`/`local_ip`/`local_dns_hostname`/`mdns_hostname`/
    `tailscale_ip` variables the composer derived it from, so the identical
    priority chain (`select_local_route`) reproduces the same route here
    without needing a general Jinja evaluator (explicitly out of scope).
    Returns `None` (never a guess) when neither representation resolves.
    """
    ansible_host = host_vars.get("ansible_host")
    if isinstance(ansible_host, str) and ansible_host.strip() and "{{" not in ansible_host:
        return ansible_host.strip()

    connection_path = host_vars.get("connection_path")
    if connection_path == "tailscale":
        tailscale_ip = host_vars.get("tailscale_ip")
        return tailscale_ip.strip() if isinstance(tailscale_ip, str) and tailscale_ip.strip() else None
    if connection_path == "local":
        local_ip = host_vars.get("local_ip")
        local_dns_hostname = host_vars.get("local_dns_hostname")
        mdns_hostname = host_vars.get("mdns_hostname")
        return select_local_route(
            local_ip=local_ip if isinstance(local_ip, str) else None,
            local_dns_hostname=local_dns_hostname if isinstance(local_dns_hostname, str) else None,
            mdns_hostname=mdns_hostname if isinstance(mdns_hostname, str) else None,
            inventory_hostname=hostname,
        )
    return None


def check_inventory_ssh_preflight(
    known_hosts_path: Path,
    keyscan_timeout_seconds: float,
    target_hosts: list[str],
    host_vars_by_host: dict[str, dict[str, Any]],
    probe: SshProbeRunner,
) -> list[SshPreflightEntry]:
    """Read-only: managed-store enrollment + a matching currently-offered key, per host.

    Callers must run `validate_inventory_trust_contract` for every host
    first -- this function trusts `host_vars_by_host[host]["nintent_desired_node_id"]`
    to already be a valid UUID and the alias to already match it.
    """
    raw_lines = read_raw_lines(known_hosts_path)
    entries: list[SshPreflightEntry] = []
    for host in sorted(target_hosts):
        host_vars = host_vars_by_host.get(host, {})
        alias = derive_host_key_alias(host_vars["nintent_desired_node_id"])
        lookup_name = managed_lookup_name(alias)
        managed = entries_for_lookup_name(raw_lines, lookup_name)
        if not managed:
            entries.append(SshPreflightEntry(slug=host, alias=alias, status=STATUS_UNENROLLED))
            continue

        route = resolve_route_from_host_vars(host_vars, host)
        if not route:
            entries.append(
                SshPreflightEntry(slug=host, alias=alias, status=STATUS_UNREACHABLE, detail="no_resolvable_route")
            )
            continue

        # `validate_inventory_trust_contract` has already rejected any
        # non-integer/out-of-range `ansible_port` for every host before this
        # function runs, so a present value here is always a valid port
        # (fix_sshkey3 Step 1 contract item 2) -- this never coerces a bad
        # value to 22, it only supplies the real Ansible default when the
        # variable is absent.
        port = host_vars.get("ansible_port")
        port = port if isinstance(port, int) else 22
        try:
            offered = scan_offered_keys(probe, route, port, keyscan_timeout_seconds)
        except SshTrustError as exc:
            entries.append(SshPreflightEntry(slug=host, alias=alias, status=STATUS_UNREACHABLE, detail=str(exc)))
            continue

        managed_pairs = {(e.key_type, e.key_blob_b64) for e in managed}
        offered_pairs = {(k.key_type, k.key_blob_b64) for k in offered}
        if managed_pairs & offered_pairs:
            entries.append(SshPreflightEntry(slug=host, alias=alias, status=STATUS_READY))
        else:
            entries.append(SshPreflightEntry(slug=host, alias=alias, status=STATUS_MISMATCH))
    return entries
