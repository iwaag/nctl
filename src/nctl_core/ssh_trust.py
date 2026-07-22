"""Pure, testable helpers for the stable-alias SSH trust contract (fix_sshkey Step 1).

These helpers derive an OpenSSH `HostKeyAlias` from the immutable DesiredNode
UUID -- never from a hostname, IP, mDNS name, Device ID, or MAC address -- and
provide the small amount of known_hosts/fingerprint parsing needed by
enrollment and preflight. They do no I/O and own no file locking; callers in
`ssh_enroll.py` / the reconcile preflight are responsible for that.
"""

from __future__ import annotations

import base64
import hashlib
import re
import shlex
import uuid
from dataclasses import dataclass

ALIAS_PREFIX = "nctl-node-"

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


class SshTrustError(ValueError):
    """Raised for malformed node IDs, aliases, or known_hosts input."""


def validate_desired_node_id(node_id: str) -> str:
    """Validate `node_id` is a canonical UUID string and return it lowercased."""
    if not isinstance(node_id, str) or not _UUID_RE.match(node_id):
        raise SshTrustError(f"not a valid DesiredNode UUID: {node_id!r}")
    return str(uuid.UUID(node_id))


def derive_host_key_alias(node_id: str) -> str:
    """Derive the stable `HostKeyAlias` for a DesiredNode.

    The alias depends only on the DesiredNode UUID. It must never be derived
    from `ansible_host`, a slug, a Device ID, or a MAC address: those may
    change or be relinked to different hardware, while the DesiredNode UUID
    represents the same logical node slot for the life of the plan.
    """
    return f"{ALIAS_PREFIX}{validate_desired_node_id(node_id)}"


def managed_lookup_name(alias: str) -> str:
    """Return the nctl-managed known_hosts lookup name for `alias`.

    Always the bare alias, independent of `ansible_port`: when
    `HostKeyAlias` is configured, OpenSSH uses the alias itself as the
    known_hosts lookup name and never appends the connection port (see
    `ssh_config(5)` HostKeyAlias). A managed store keyed any other way would
    disagree with the real SSH connection on a non-default port.
    """
    if not alias:
        raise SshTrustError("alias must not be empty")
    return alias


def legacy_lookup_name(effective_host: str, effective_port: int, host_key_alias: str | None) -> str:
    """Return the known_hosts lookup name OpenSSH uses for a normal (non-managed) connection.

    Mirrors portable OpenSSH's `get_hostfile_hostname_ipaddr()`: if an
    effective `HostKeyAlias` is active, it is used verbatim with no port
    suffix; otherwise the effective host is used bare on port 22 and as
    `[host]:port` for any other port. Only legacy known_hosts promotion
    (`nctl ssh enroll --from-known-hosts`) uses this -- never the managed
    store, which is always looked up via `managed_lookup_name`.
    """
    if host_key_alias:
        return host_key_alias
    if not effective_host:
        raise SshTrustError("effective_host must not be empty")
    if effective_port == 22:
        return effective_host
    if not (1 <= effective_port <= 65535):
        raise SshTrustError(f"invalid port: {effective_port}")
    return f"[{effective_host}]:{effective_port}"


@dataclass(frozen=True)
class EffectiveSshConfig:
    """The subset of `ssh -G` output needed to compute a legacy lookup name."""

    hostname: str
    port: int
    host_key_alias: str | None
    user_known_hosts_files: tuple[str, ...]


def parse_effective_ssh_config(output: str) -> EffectiveSshConfig:
    """Parse `ssh -G` output into the fields `legacy_lookup_name` needs.

    `ssh -G` prints one resolved directive per line as `key value...`
    (lowercase key). Unrecognized directives are ignored. `port` defaults to
    22 if OpenSSH did not print a `port` line (it always does in practice,
    but callers must not crash on unexpected output).
    """
    hostname = ""
    port = 22
    host_key_alias: str | None = None
    known_hosts_files: list[str] = []
    for line in output.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        key, value = parts[0].lower(), parts[1].strip()
        if key == "hostname":
            hostname = value
        elif key == "port":
            try:
                port = int(value)
            except ValueError:
                pass
        elif key == "hostkeyalias":
            host_key_alias = value
        elif key == "userknownhostsfile":
            known_hosts_files.extend(value.split())
    return EffectiveSshConfig(
        hostname=hostname,
        port=port,
        host_key_alias=host_key_alias,
        user_known_hosts_files=tuple(known_hosts_files),
    )


def build_ansible_ssh_common_args(alias: str, known_hosts_path: str) -> str:
    """Build the closed, controller-generated `ansible_ssh_common_args` value.

    Every option here is fixed policy, not user-configurable text: callers
    must never splice in values from nintent or other free-form config.
    """
    if not alias:
        raise SshTrustError("alias must not be empty")
    if not known_hosts_path:
        raise SshTrustError("known_hosts_path must not be empty")
    options = [
        ("HostKeyAlias", alias),
        ("UserKnownHostsFile", known_hosts_path),
        ("StrictHostKeyChecking", "yes"),
        ("CheckHostIP", "no"),
        ("UpdateHostKeys", "no"),
    ]
    parts = [f"-o {shlex.quote(f'{key}={value}')}" for key, value in options]
    return " ".join(parts)


def compute_sha256_fingerprint(key_blob_b64: str) -> str:
    """Compute the OpenSSH-format SHA256 fingerprint of a base64 public-key blob."""
    try:
        raw = base64.b64decode(key_blob_b64, validate=True)
    except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
        raise SshTrustError(f"malformed base64 key blob: {exc}") from exc
    if not raw:
        raise SshTrustError("empty key blob")
    digest = hashlib.sha256(raw).digest()
    encoded = base64.b64encode(digest).decode("ascii").rstrip("=")
    return f"SHA256:{encoded}"


@dataclass(frozen=True)
class ParsedHostKeyLine:
    """One ordinary (non-marker) known_hosts / ssh-keyscan entry."""

    names: tuple[str, ...]
    key_type: str
    key_blob_b64: str
    comment: str | None = None


_KNOWN_KEY_TYPES = frozenset(
    {
        "ssh-rsa",
        "ssh-dss",
        "ssh-ed25519",
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
        "sk-ssh-ed25519@openssh.com",
        "sk-ecdsa-sha2-nistp256@openssh.com",
    }
)


def parse_known_hosts_line(line: str) -> ParsedHostKeyLine | None:
    """Parse one known_hosts/keyscan line into an ordinary host-key entry.

    Returns `None` for blank lines, comments, and CA/revocation markers
    (`@cert-authority`, `@revoked`): those are never treated as an ordinary
    trusted host key by this helper. Raises `SshTrustError` for a line that
    looks like a host-key entry but is malformed (missing fields, unknown key
    type, or unparsable base64) so callers do not silently skip a corrupt
    trust-store line.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("@"):
        return None
    fields = stripped.split()
    if len(fields) < 3:
        raise SshTrustError(f"malformed known_hosts line: {line!r}")
    names_field, key_type, key_blob_b64, *rest = fields
    if key_type not in _KNOWN_KEY_TYPES:
        raise SshTrustError(f"unknown key type in known_hosts line: {key_type!r}")
    try:
        base64.b64decode(key_blob_b64, validate=True)
    except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
        raise SshTrustError(f"malformed base64 key blob: {exc}") from exc
    names = tuple(names_field.split(","))
    comment = " ".join(rest) if rest else None
    return ParsedHostKeyLine(names=names, key_type=key_type, key_blob_b64=key_blob_b64, comment=comment)


def is_hashed_hostname_entry(names_field: str) -> bool:
    """Return True if a raw known_hosts hostnames field uses HashKnownHosts form."""
    return names_field.startswith("|1|")


@dataclass(frozen=True)
class ManagedEntry:
    """One entry in the alias-keyed managed known_hosts store."""

    alias: str
    key_type: str
    key_blob_b64: str


def find_managed_entry(
    entries: list[ManagedEntry], alias: str, key_type: str | None = None
) -> ManagedEntry | None:
    """Return the managed entry for `alias` (optionally filtered by key type).

    Lookup is by exact alias match only -- never by endpoint name/address --
    so a managed store never accidentally trusts a key via `.local`,
    `.home.arpa`, or an IP that happens to share a key with the alias.
    """
    for entry in entries:
        if entry.alias == alias and (key_type is None or entry.key_type == key_type):
            return entry
    return None
