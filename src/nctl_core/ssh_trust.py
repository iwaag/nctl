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


def derive_lookup_name(alias: str, port: int = 22) -> str:
    """Return the OpenSSH-compatible known_hosts lookup name for `alias`/`port`.

    Port 22 uses the bare alias; any other port uses the bracketed
    `[alias]:port` form, matching OpenSSH's own non-default-port host-key
    naming convention.
    """
    if not alias:
        raise SshTrustError("alias must not be empty")
    if port == 22:
        return alias
    if not (1 <= port <= 65535):
        raise SshTrustError(f"invalid port: {port}")
    return f"[{alias}]:{port}"


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
