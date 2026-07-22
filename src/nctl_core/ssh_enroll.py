"""Explicit SSH host-key enrollment (fix_sshkey Step 2, Design Decision 4).

Enrollment is the only way to add or replace an entry in the nctl-managed
known_hosts store described in `devdocs/small/fix_sshkey/plan.md`. An
unverified `ssh-keyscan` result is never sufficient on its own to mark a key
trusted, even with `--yes`: a verified source (`--from-known-hosts` matching
an already-trusted `.local` entry, or an explicit `--fingerprint`) is
required before any write.

Reuses `hosts_intent.select_mdns_endpoint` so the bootstrap endpoint
preference is defined in exactly one place.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, Field

from nctl_core.artifacts import ArtifactError, atomic_write_private
from nctl_core.config import Config, ConfigError
from nctl_core.events import OperationLog
from nctl_core.hosts_intent import select_mdns_endpoint
from nctl_core.nautobot import NautobotClient, NautobotError
from nctl_core.output import Envelope, EnvelopeError
from nctl_core.reconcile.lock import ReconcileLockError, acquire_reconcile_lock
from nctl_core.sources.desired import DesiredNode, DesiredSnapshot, fetch_desired_snapshot
from nctl_core.ssh_trust import (
    ManagedEntry,
    ParsedHostKeyLine,
    SshTrustError,
    compute_sha256_fingerprint,
    derive_host_key_alias,
    legacy_lookup_name,
    managed_lookup_name,
    parse_effective_ssh_config,
    parse_known_hosts_line,
)

SSH_ENROLL_SCHEMA = "nctl.ssh.enroll.v1"
DEFAULT_SSH_PORT = 22


class SshStoreReadError(Exception):
    """Raised when the managed known_hosts file cannot be read (I/O, permission, encoding)."""


class SshEnrollData(BaseModel):
    operation_id: str = ""
    mode: str = "plan"
    action: str = ""
    applied: bool = False
    node_id: str = ""
    node_slug: str = ""
    endpoint: str = ""
    port: int = DEFAULT_SSH_PORT
    alias: str = ""
    lookup_name: str = ""
    known_hosts_file: str = ""
    verified_source: str | None = None
    managed_keys: list[str] = Field(default_factory=list)
    offered_keys: list[str] = Field(default_factory=list)
    replaced: bool = False


@dataclass
class SshProbeRunner:
    """Injected subprocess boundary for `ssh-keyscan`, `ssh -G`, and `ssh-keygen -F`.

    Every real implementation invokes argv directly (never a shell); tests
    inject fakes so they never depend on the developer's real SSH files or
    network.
    """

    keyscan: Callable[[str, int, float], subprocess.CompletedProcess[str]]
    effective_config: Callable[[str, int], subprocess.CompletedProcess[str]]
    keygen_find: Callable[[Path, str], subprocess.CompletedProcess[str]]


def _default_keyscan(host: str, port: int, timeout_seconds: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["ssh-keyscan", "-p", str(port), "-T", str(max(1, int(timeout_seconds))), host],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_seconds + 5,
    )


def _default_effective_config(host: str, port: int) -> subprocess.CompletedProcess[str]:
    """Run `ssh -G -p <port> <host>` to discover the effective, port-aware OpenSSH config.

    Passing `-p` explicitly matters: an `ssh_config` `Host` block can key its
    `Port`/`HostKeyAlias` override on the connection port, so probing without
    the real port can report the wrong effective values.
    """
    return subprocess.run(
        ["ssh", "-G", "-p", str(port), host], capture_output=True, text=True, check=False, timeout=10
    )


def _default_keygen_find(known_hosts_path: Path, hostname: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["ssh-keygen", "-F", hostname, "-f", str(known_hosts_path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def default_ssh_probe_runner() -> SshProbeRunner:
    return SshProbeRunner(
        keyscan=_default_keyscan,
        effective_config=_default_effective_config,
        keygen_find=_default_keygen_find,
    )


def _resolve_node(snapshot: DesiredSnapshot, host: str) -> tuple[DesiredNode | None, EnvelopeError | None]:
    node = next((n for n in snapshot.nodes if n.slug == host), None)
    if node is None:
        return None, EnvelopeError(code="unknown_host", message=f"no DesiredNode with slug {host!r}")
    return node, None


def _resolve_endpoint_and_port(
    snapshot: DesiredSnapshot, node: DesiredNode
) -> tuple[str | None, int, EnvelopeError | None]:
    endpoints = [e for e in snapshot.endpoints if e.node_id == node.id]
    endpoint = select_mdns_endpoint(endpoints)
    if endpoint is None:
        return None, DEFAULT_SSH_PORT, EnvelopeError(
            code="node_without_mdns", message=f"DesiredNode {node.slug!r} has no mDNS endpoint to enroll from"
        )
    override = next((o for o in snapshot.operational_overrides if o.node_id == node.id), None)
    port = override.ansible_port if override and override.ansible_port else DEFAULT_SSH_PORT
    return endpoint.mdns_name, port, None


def scan_offered_keys(
    probe: SshProbeRunner, host: str, port: int, timeout_seconds: float
) -> list[ParsedHostKeyLine]:
    """Observe currently offered public keys via `ssh-keyscan`.

    Bounded timeout, argv-based subprocess execution. The result is never
    sufficient on its own to mark a key trusted -- see `select_verified_offered_keys`.
    """
    try:
        completed = probe.keyscan(host, port, timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        raise SshTrustError(f"ssh-keyscan timed out after {timeout_seconds}s for {host}:{port}") from exc
    if completed.returncode != 0 and not completed.stdout.strip():
        raise SshTrustError(f"ssh-keyscan failed for {host}:{port}: {completed.stderr.strip()}")
    keys: list[ParsedHostKeyLine] = []
    for line in completed.stdout.splitlines():
        parsed = parse_known_hosts_line(line)
        if parsed is not None:
            keys.append(parsed)
    return keys


def find_legacy_trusted_keys(probe: SshProbeRunner, endpoint: str, port: int) -> list[ParsedHostKeyLine]:
    """Resolve the effective, port-aware OpenSSH lookup name and known_hosts files for `endpoint`.

    Uses `ssh -G -p <port>` (to discover the effective `hostname`, `port`,
    `hostkeyalias`, and `userknownhostsfile`, since any of these may differ
    from the literal `endpoint`/`~/.ssh/known_hosts`) and `ssh-keygen -F`
    (which also matches hashed hostname entries) rather than assuming a
    single well-known file path or lookup name. Searches under
    `legacy_lookup_name`, matching a normal OpenSSH endpoint connection --
    never the managed alias-keyed store, which is a separate lookup name.
    """
    completed = probe.effective_config(endpoint, port)
    effective = parse_effective_ssh_config(completed.stdout)
    lookup_name = legacy_lookup_name(
        effective.hostname or endpoint, effective.port or port, effective.host_key_alias
    )
    trusted: list[ParsedHostKeyLine] = []
    for raw_path in effective.user_known_hosts_files:
        path = Path(raw_path).expanduser()
        if not path.is_file():
            continue
        found = probe.keygen_find(path, lookup_name)
        for line in found.stdout.splitlines():
            if line.startswith("#"):
                continue
            parsed = parse_known_hosts_line(line)
            if parsed is not None:
                trusted.append(parsed)
    return trusted


def select_verified_offered_keys(
    offered: list[ParsedHostKeyLine],
    *,
    legacy_trusted: list[ParsedHostKeyLine] | None = None,
    fingerprints: list[str] | None = None,
) -> list[ParsedHostKeyLine]:
    """Return only offered keys backed by an exact previously-trusted key or matching fingerprint."""
    legacy_pairs = {(k.key_type, k.key_blob_b64) for k in (legacy_trusted or [])}
    fingerprint_set = set(fingerprints or [])
    verified = []
    for key in offered:
        if (key.key_type, key.key_blob_b64) in legacy_pairs:
            verified.append(key)
            continue
        if fingerprint_set and compute_sha256_fingerprint(key.key_blob_b64) in fingerprint_set:
            verified.append(key)
    return verified


def read_raw_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    try:
        return path.read_text().splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise SshStoreReadError(f"cannot read managed known_hosts file {path}: {exc}") from exc


def entries_for_lookup_name(lines: list[str], lookup_name: str) -> list[ManagedEntry]:
    entries = []
    for line in lines:
        try:
            parsed = parse_known_hosts_line(line)
        except SshTrustError:
            continue
        if parsed is not None and parsed.names == (lookup_name,):
            entries.append(
                ManagedEntry(alias=lookup_name, key_type=parsed.key_type, key_blob_b64=parsed.key_blob_b64)
            )
    return entries


def _lines_excluding_lookup_name(lines: list[str], lookup_name: str) -> list[str]:
    """Keep every line except ordinary entries for `lookup_name` -- comments/other aliases untouched."""
    kept = []
    for line in lines:
        try:
            parsed = parse_known_hosts_line(line)
        except SshTrustError:
            kept.append(line)
            continue
        if parsed is not None and parsed.names == (lookup_name,):
            continue
        kept.append(line)
    return kept


def _obsolete_alias_port_lookup_name(alias: str, port: int) -> str | None:
    """Return the obsolete `[alias]:port` managed-store name to purge, if any.

    Before fix_sshkey2, non-default-port enrollment wrote `[alias]:port`
    instead of the bare alias. `None` at port 22 (the bare alias there is
    already the correct current form, never a malformed leftover).
    """
    if port == 22:
        return None
    return f"[{alias}]:{port}"


def _write_managed_file(
    path: Path, lines_without_entry: list[str], lookup_name: str, slug: str, keys: list[ParsedHostKeyLine]
) -> None:
    output_lines = list(lines_without_entry)
    for key in keys:
        output_lines.append(f"{lookup_name} {key.key_type} {key.key_blob_b64} nctl:{slug}")
    content = "\n".join(output_lines)
    if content:
        content += "\n"
    atomic_write_private(path, content.encode("utf-8"))


def build_ssh_enroll(
    cfg: Config,
    host: str,
    *,
    from_known_hosts: bool = False,
    fingerprints: list[str] | None = None,
    replace: bool = False,
    apply_changes: bool = False,
    probe: SshProbeRunner | None = None,
    operation_id: str | None = None,
) -> Envelope[SshEnrollData]:
    probe = probe or default_ssh_probe_runner()
    fingerprints = fingerprints or []
    op = OperationLog("ssh enroll", cfg.events.resolved_log_dir(), operation_id=operation_id)
    op.emit("started", "ssh enroll started", host=host)
    data = SshEnrollData(operation_id=op.operation_id, mode="apply" if apply_changes else "plan")

    try:
        token = cfg.nautobot.resolve_token()
    except ConfigError as exc:
        return _fail(op, data, "nautobot_token_error", str(exc))

    client = NautobotClient(cfg.nautobot.url, token)
    try:
        return _build_ssh_enroll_with_client(
            cfg,
            host,
            client,
            op,
            data,
            from_known_hosts=from_known_hosts,
            fingerprints=fingerprints,
            replace=replace,
            apply_changes=apply_changes,
            probe=probe,
        )
    finally:
        client.close()


def _build_ssh_enroll_with_client(
    cfg: Config,
    host: str,
    client: NautobotClient,
    op: OperationLog,
    data: SshEnrollData,
    *,
    from_known_hosts: bool,
    fingerprints: list[str],
    replace: bool,
    apply_changes: bool,
    probe: SshProbeRunner,
) -> Envelope[SshEnrollData]:
    try:
        snapshot = fetch_desired_snapshot(client)
    except NautobotError as exc:
        return _fail(op, data, "nautobot_fetch_failed", str(exc))

    node, error = _resolve_node(snapshot, host)
    if error is not None:
        return _fail(op, data, error.code, error.message)
    data.node_id = node.id
    data.node_slug = node.slug

    endpoint, port, error = _resolve_endpoint_and_port(snapshot, node)
    if error is not None:
        return _fail(op, data, error.code, error.message)
    data.endpoint = endpoint
    data.port = port

    alias = derive_host_key_alias(node.id)
    lookup_name = managed_lookup_name(alias)
    known_hosts_path = cfg.resolved_ssh_known_hosts_file()
    data.alias = alias
    data.lookup_name = lookup_name
    data.known_hosts_file = str(known_hosts_path)

    def _plan_and_maybe_apply() -> Envelope[SshEnrollData]:
        try:
            offered = scan_offered_keys(probe, endpoint, port, cfg.ssh.keyscan_timeout_seconds)
        except SshTrustError as exc:
            return _fail(op, data, "ssh_probe_failed", str(exc))
        data.offered_keys = [f"{k.key_type} {compute_sha256_fingerprint(k.key_blob_b64)}" for k in offered]

        legacy_trusted: list[ParsedHostKeyLine] = []
        if from_known_hosts:
            try:
                legacy_trusted = find_legacy_trusted_keys(probe, endpoint, port)
            except SshTrustError as exc:
                return _fail(op, data, "ssh_probe_failed", str(exc))

        verified_keys = select_verified_offered_keys(
            offered,
            legacy_trusted=legacy_trusted if from_known_hosts else None,
            fingerprints=fingerprints,
        )
        if not verified_keys:
            op.emit("decided", "no verified key", action="unverified")
            data.action = "unverified"
            return Envelope.build(
                SSH_ENROLL_SCHEMA,
                data,
                [
                    EnvelopeError(
                        code="host_key_unverified",
                        message=(
                            "no previously trusted key (--from-known-hosts) or matching "
                            "--fingerprint was found among the currently offered keys"
                        ),
                    )
                ],
            )
        if from_known_hosts and legacy_trusted:
            data.verified_source = "from_known_hosts"
        elif fingerprints:
            data.verified_source = "fingerprint"

        try:
            raw_lines = read_raw_lines(known_hosts_path)
        except SshStoreReadError as exc:
            return _fail(op, data, "ssh_store_read_failed", str(exc))
        existing = entries_for_lookup_name(raw_lines, lookup_name)
        data.managed_keys = [f"{e.key_type} {compute_sha256_fingerprint(e.key_blob_b64)}" for e in existing]

        existing_pairs = {(e.key_type, e.key_blob_b64) for e in existing}
        verified_pairs = {(k.key_type, k.key_blob_b64) for k in verified_keys}

        if not existing:
            action = "enroll"
        elif existing_pairs == verified_pairs:
            action = "noop"
        else:
            action = "conflict"
        data.action = action

        if action == "noop":
            op.emit("decided", "already enrolled", action=action)
            return Envelope.build(SSH_ENROLL_SCHEMA, data)

        if action == "conflict" and not replace:
            op.emit("decided", "conflicting key", action=action)
            return Envelope.build(
                SSH_ENROLL_SCHEMA,
                data,
                [
                    EnvelopeError(
                        code="host_key_conflict",
                        message=(
                            f"a different key is already enrolled for alias {alias}; "
                            "pass --replace with a verified source to replace it"
                        ),
                    )
                ],
            )

        if not apply_changes:
            op.emit("planned", "dry plan only, no write", action=action)
            return Envelope.build(SSH_ENROLL_SCHEMA, data)

        lines_without_entry = _lines_excluding_lookup_name(raw_lines, lookup_name)
        obsolete_name = _obsolete_alias_port_lookup_name(alias, port)
        if obsolete_name is not None:
            lines_without_entry = _lines_excluding_lookup_name(lines_without_entry, obsolete_name)
        try:
            _write_managed_file(known_hosts_path, lines_without_entry, lookup_name, node.slug, verified_keys)
        except (OSError, ArtifactError) as exc:
            return _fail(op, data, "ssh_store_write_failed", str(exc))

        data.applied = True
        data.replaced = action == "conflict"
        op.emit("applied", "managed known_hosts entry written", action=action)
        return Envelope.build(SSH_ENROLL_SCHEMA, data)

    if not apply_changes:
        return _plan_and_maybe_apply()

    try:
        with acquire_reconcile_lock(cfg.resolved_ssh_lock_path()):
            return _plan_and_maybe_apply()
    except ReconcileLockError as exc:
        return _fail(op, data, "ssh_lock_contention", str(exc))


def _fail(op: OperationLog, data: SshEnrollData, code: str, message: str) -> Envelope[SshEnrollData]:
    op.emit("failed", message, code=code)
    return Envelope.build(SSH_ENROLL_SCHEMA, data, [EnvelopeError(code=code, message=message)])


def render_ssh_enroll_text(envelope: Envelope[SshEnrollData]) -> str:
    d = envelope.data
    lines = [
        f"ssh enroll: {d.mode} action={d.action} node={d.node_slug} endpoint={d.endpoint} port={d.port}",
        f"  alias: {d.alias}",
        f"  lookup name: {d.lookup_name}",
        f"  known_hosts_file: {d.known_hosts_file}",
        f"  verified_source: {d.verified_source or '-'}",
        f"  offered keys: {', '.join(d.offered_keys) or '-'}",
        f"  managed keys: {', '.join(d.managed_keys) or '-'}",
        f"  applied: {d.applied} replaced: {d.replaced}",
    ]
    for error in envelope.errors:
        lines.append(f"  error[{error.code}]: {error.message}")
    return "\n".join(lines)
