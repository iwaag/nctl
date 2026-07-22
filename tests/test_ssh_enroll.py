from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import nctl_core.ssh_enroll as ssh_enroll
from nctl_core.config import Config
from nctl_core.ssh_enroll import (
    SSH_ENROLL_SCHEMA,
    SshProbeRunner,
    SshStoreReadError,
    build_ssh_enroll,
    load_managed_ssh_store,
)
from nctl_core.sources.desired import DesiredEndpoint, DesiredNode, DesiredNodeOperationalOverride, DesiredSnapshot
from nctl_core.ssh_trust import compute_sha256_fingerprint, derive_host_key_alias

NODE_ID = "27818c12-fe15-4c9f-83d0-7949523f6c33"
KEY_BLOB = "QUFBQUMzTnphQzFsWkRJMU5URTVBQUFBSUZmYWtlZWQyNTUxOWtleWJ5dGVzMDAwMDAwMDAwMDAwMDAwMA=="
OTHER_KEY_BLOB = "QUFBQUMzTnphQzFsWkRJMU5URTVBQUFBSUZmYWtlZWQyNTUxOWRpZmZlcmVudGtleWJ5dGVzMDAwMDA="


def _config(tmp_path: Path) -> Config:
    config_path = tmp_path / "nctl.toml"
    config_path.write_text(
        f"""
[nautobot]
url = "http://nautobot.test"

[inventory]
dumps_dir = "{tmp_path / 'dumps'}"

[events]
log_dir = "{tmp_path / 'events'}"

[ansible]
playbook_dir = "{tmp_path / 'ansible_agdev'}"
inventory = "inventories/generated/hosts_intent.yml"

[repo]
root = "{tmp_path}"

[ssh]
known_hosts_file = "{tmp_path / 'ssh' / 'known_hosts'}"
lock_path = "{tmp_path / 'ssh.lock'}"
"""
    )
    (tmp_path / "ansible_agdev" / "inventories" / "generated").mkdir(parents=True)
    (tmp_path / "ansible_agdev" / "inventories" / "generated" / "hosts_intent.yml").write_text("all: {}\n")
    return Config.load(config_path)


def _snapshot(port: int | None = None) -> DesiredSnapshot:
    overrides = []
    if port is not None:
        overrides.append(
            DesiredNodeOperationalOverride(id="override-1", node_id=NODE_ID, ansible_port=port)
        )
    return DesiredSnapshot(
        nodes=[
            DesiredNode(id=NODE_ID, slug="agdnsmasq", name="agdnsmasq", lifecycle="active", node_type="device"),
            DesiredNode(id="00000000-0000-0000-0000-000000000002", slug="no-mdns", name="No mDNS", lifecycle="active", node_type="device"),
        ],
        endpoints=[
            DesiredEndpoint(
                id="endpoint-1",
                name="primary",
                endpoint_type="primary",
                node_id=NODE_ID,
                node_slug="agdnsmasq",
                mdns_name="agdnsmasq.local",
            ),
        ],
        operational_overrides=overrides,
    )


class _FakeNautobotClient:
    def close(self) -> None:
        pass


def _patch_snapshot(monkeypatch, port: int | None = None) -> None:
    monkeypatch.setattr(ssh_enroll, "NautobotClient", lambda url, token: _FakeNautobotClient())
    monkeypatch.setattr(ssh_enroll, "fetch_desired_snapshot", lambda client: _snapshot(port))


def _keyscan_line(key_blob: str = KEY_BLOB) -> str:
    return f"agdnsmasq.local ssh-ed25519 {key_blob}\n"


def _probe(
    *,
    keyscan_stdout: str = "",
    keyscan_returncode: int = 0,
    legacy_lines: list[str] | None = None,
    known_hosts_files: list[Path] | None = None,
    keyscan_raises: Exception | None = None,
    effective_host_key_alias: str | None = None,
    effective_config_raises: Exception | None = None,
    effective_config_returncode: int = 0,
    keygen_find_raises: Exception | None = None,
) -> SshProbeRunner:
    def keyscan(host: str, port: int, timeout: float) -> subprocess.CompletedProcess[str]:
        if keyscan_raises is not None:
            raise keyscan_raises
        return subprocess.CompletedProcess(args=["ssh-keyscan"], returncode=keyscan_returncode, stdout=keyscan_stdout, stderr="")

    def effective_config(host: str, port: int) -> subprocess.CompletedProcess[str]:
        if effective_config_raises is not None:
            raise effective_config_raises
        lines = [f"hostname {host}", f"port {port}"]
        if effective_host_key_alias:
            lines.append(f"hostkeyalias {effective_host_key_alias}")
        if known_hosts_files:
            lines.append("userknownhostsfile " + " ".join(str(p) for p in known_hosts_files))
        return subprocess.CompletedProcess(
            args=["ssh", "-G"], returncode=effective_config_returncode, stdout="\n".join(lines) + "\n", stderr=""
        )

    def keygen_find(path: Path, hostname: str) -> subprocess.CompletedProcess[str]:
        if keygen_find_raises is not None:
            raise keygen_find_raises
        stdout = "\n".join(legacy_lines or []) + ("\n" if legacy_lines else "")
        return subprocess.CompletedProcess(args=["ssh-keygen"], returncode=0, stdout=stdout, stderr="")

    return SshProbeRunner(keyscan=keyscan, effective_config=effective_config, keygen_find=keygen_find)


def test_unknown_host_reports_usage_error(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _patch_snapshot(monkeypatch)
    envelope = build_ssh_enroll(cfg, "does-not-exist", probe=_probe())
    assert not envelope.ok
    assert envelope.errors[0].code == "unknown_host"


def test_node_without_mdns_is_reported(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _patch_snapshot(monkeypatch)
    envelope = build_ssh_enroll(cfg, "no-mdns", probe=_probe())
    assert not envelope.ok
    assert envelope.errors[0].code == "node_without_mdns"


def test_unverified_scan_cannot_be_applied_even_with_yes(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _patch_snapshot(monkeypatch)
    probe = _probe(keyscan_stdout=_keyscan_line())
    envelope = build_ssh_enroll(cfg, "agdnsmasq", apply_changes=True, probe=probe)
    assert not envelope.ok
    assert envelope.errors[0].code == "host_key_unverified"
    assert not cfg.resolved_ssh_known_hosts_file().exists()


def test_matching_explicit_fingerprint_can_be_applied(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _patch_snapshot(monkeypatch)
    fingerprint = compute_sha256_fingerprint(KEY_BLOB)
    probe = _probe(keyscan_stdout=_keyscan_line())
    envelope = build_ssh_enroll(
        cfg, "agdnsmasq", fingerprints=[fingerprint], apply_changes=True, probe=probe
    )
    assert envelope.ok, envelope.errors
    assert envelope.data.action == "enroll"
    assert envelope.data.applied is True
    assert envelope.data.verified_source == "fingerprint"
    alias = derive_host_key_alias(NODE_ID)
    content = cfg.resolved_ssh_known_hosts_file().read_text()
    assert alias in content
    assert KEY_BLOB in content


def test_matching_plain_legacy_entry_can_be_promoted(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _patch_snapshot(monkeypatch)
    legacy_file = tmp_path / "known_hosts"
    legacy_file.write_text(_keyscan_line())
    probe = _probe(keyscan_stdout=_keyscan_line(), legacy_lines=[_keyscan_line().strip()], known_hosts_files=[legacy_file])
    envelope = build_ssh_enroll(cfg, "agdnsmasq", from_known_hosts=True, apply_changes=True, probe=probe)
    assert envelope.ok, envelope.errors
    assert envelope.data.verified_source == "from_known_hosts"
    assert envelope.data.applied is True


def test_matching_hashed_legacy_entry_can_be_promoted(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _patch_snapshot(monkeypatch)
    legacy_file = tmp_path / "known_hosts"
    legacy_file.write_text("|1|abcd1234ABCD==|efghsalt5678ABCD== ssh-ed25519 " + KEY_BLOB + "\n")
    probe = _probe(
        keyscan_stdout=_keyscan_line(),
        legacy_lines=["|1|abcd1234ABCD==|efghsalt5678ABCD== ssh-ed25519 " + KEY_BLOB],
        known_hosts_files=[legacy_file],
    )
    envelope = build_ssh_enroll(cfg, "agdnsmasq", from_known_hosts=True, apply_changes=True, probe=probe)
    assert envelope.ok, envelope.errors
    assert envelope.data.applied is True


def test_legacy_entry_not_matching_offered_key_fails(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _patch_snapshot(monkeypatch)
    legacy_file = tmp_path / "known_hosts"
    legacy_line = f"agdnsmasq.local ssh-ed25519 {OTHER_KEY_BLOB}"
    legacy_file.write_text(legacy_line + "\n")
    probe = _probe(keyscan_stdout=_keyscan_line(), legacy_lines=[legacy_line], known_hosts_files=[legacy_file])
    envelope = build_ssh_enroll(cfg, "agdnsmasq", from_known_hosts=True, apply_changes=True, probe=probe)
    assert not envelope.ok
    assert envelope.errors[0].code == "host_key_unverified"


def test_existing_identical_entry_is_noop(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _patch_snapshot(monkeypatch)
    fingerprint = compute_sha256_fingerprint(KEY_BLOB)
    probe = _probe(keyscan_stdout=_keyscan_line())
    first = build_ssh_enroll(cfg, "agdnsmasq", fingerprints=[fingerprint], apply_changes=True, probe=probe)
    assert first.ok and first.data.applied is True

    second = build_ssh_enroll(cfg, "agdnsmasq", fingerprints=[fingerprint], apply_changes=True, probe=probe)
    assert second.ok
    assert second.data.action == "noop"
    assert second.data.applied is False


def test_conflict_fails_without_replace(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _patch_snapshot(monkeypatch)
    fingerprint = compute_sha256_fingerprint(KEY_BLOB)
    probe = _probe(keyscan_stdout=_keyscan_line())
    first = build_ssh_enroll(cfg, "agdnsmasq", fingerprints=[fingerprint], apply_changes=True, probe=probe)
    assert first.ok

    other_fingerprint = compute_sha256_fingerprint(OTHER_KEY_BLOB)
    other_probe = _probe(keyscan_stdout=_keyscan_line(OTHER_KEY_BLOB))
    conflicting = build_ssh_enroll(
        cfg, "agdnsmasq", fingerprints=[other_fingerprint], apply_changes=True, probe=other_probe
    )
    assert not conflicting.ok
    assert conflicting.errors[0].code == "host_key_conflict"
    content = cfg.resolved_ssh_known_hosts_file().read_text()
    assert KEY_BLOB in content
    assert OTHER_KEY_BLOB not in content


def test_conflict_with_replace_but_no_verified_source_still_fails(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _patch_snapshot(monkeypatch)
    fingerprint = compute_sha256_fingerprint(KEY_BLOB)
    probe = _probe(keyscan_stdout=_keyscan_line())
    first = build_ssh_enroll(cfg, "agdnsmasq", fingerprints=[fingerprint], apply_changes=True, probe=probe)
    assert first.ok

    other_probe = _probe(keyscan_stdout=_keyscan_line(OTHER_KEY_BLOB))
    envelope = build_ssh_enroll(
        cfg, "agdnsmasq", replace=True, apply_changes=True, probe=other_probe
    )
    assert not envelope.ok
    assert envelope.errors[0].code == "host_key_unverified"


def test_verified_replacement_changes_only_the_exact_alias_entry(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _patch_snapshot(monkeypatch)
    fingerprint = compute_sha256_fingerprint(KEY_BLOB)
    probe = _probe(keyscan_stdout=_keyscan_line())
    first = build_ssh_enroll(cfg, "agdnsmasq", fingerprints=[fingerprint], apply_changes=True, probe=probe)
    assert first.ok

    known_hosts_path = cfg.resolved_ssh_known_hosts_file()
    unrelated_alias = "nctl-node-00000000-0000-0000-0000-000000000099"
    existing = known_hosts_path.read_text()
    known_hosts_path.write_text(f"# a preserved comment\n{unrelated_alias} ssh-ed25519 {OTHER_KEY_BLOB} nctl:other\n" + existing)

    other_fingerprint = compute_sha256_fingerprint(OTHER_KEY_BLOB)
    other_probe = _probe(keyscan_stdout=_keyscan_line(OTHER_KEY_BLOB))
    envelope = build_ssh_enroll(
        cfg, "agdnsmasq", replace=True, fingerprints=[other_fingerprint], apply_changes=True, probe=other_probe
    )
    assert envelope.ok, envelope.errors
    assert envelope.data.replaced is True

    content = known_hosts_path.read_text()
    assert "# a preserved comment" in content
    assert unrelated_alias in content
    alias = derive_host_key_alias(NODE_ID)
    assert content.count(alias) == 1
    assert OTHER_KEY_BLOB in content
    # old key for our alias is gone, but the unrelated alias's OTHER_KEY_BLOB copy remains too
    assert content.count(OTHER_KEY_BLOB) == 2


def test_dry_plan_without_yes_performs_no_write(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _patch_snapshot(monkeypatch)
    fingerprint = compute_sha256_fingerprint(KEY_BLOB)
    probe = _probe(keyscan_stdout=_keyscan_line())
    envelope = build_ssh_enroll(cfg, "agdnsmasq", fingerprints=[fingerprint], apply_changes=False, probe=probe)
    assert envelope.ok
    assert envelope.data.action == "enroll"
    assert envelope.data.applied is False
    assert not cfg.resolved_ssh_known_hosts_file().exists()


def test_non_default_port_still_uses_bare_alias_in_lookup_name(tmp_path, monkeypatch):
    # fix_sshkey2 Step 1/2: the managed store is always keyed by the bare
    # alias, independent of ansible_port -- see ssh_trust.managed_lookup_name.
    cfg = _config(tmp_path)
    _patch_snapshot(monkeypatch, port=2222)
    fingerprint = compute_sha256_fingerprint(KEY_BLOB)
    probe = _probe(keyscan_stdout=_keyscan_line())
    envelope = build_ssh_enroll(cfg, "agdnsmasq", fingerprints=[fingerprint], apply_changes=False, probe=probe)
    assert envelope.data.port == 2222
    assert envelope.data.lookup_name == envelope.data.alias


def test_non_default_port_writes_bare_alias_lookup_name(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _patch_snapshot(monkeypatch, port=2222)
    fingerprint = compute_sha256_fingerprint(KEY_BLOB)
    probe = _probe(keyscan_stdout=_keyscan_line())
    envelope = build_ssh_enroll(cfg, "agdnsmasq", fingerprints=[fingerprint], apply_changes=True, probe=probe)
    assert envelope.ok, envelope.errors
    alias = derive_host_key_alias(NODE_ID)
    content = cfg.resolved_ssh_known_hosts_file().read_text()
    assert f"[{alias}]:2222" not in content
    assert content.startswith(alias + " ")


def test_stale_bracketed_entry_is_not_considered_enrolled(tmp_path, monkeypatch):
    # A managed store containing only the pre-fix_sshkey2 malformed
    # `[alias]:2222` entry must not be treated as already enrolled: the
    # runtime lookup is always the bare alias.
    cfg = _config(tmp_path)
    _patch_snapshot(monkeypatch, port=2222)
    alias = derive_host_key_alias(NODE_ID)
    known_hosts_path = cfg.resolved_ssh_known_hosts_file()
    known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
    known_hosts_path.write_text(f"[{alias}]:2222 ssh-ed25519 {KEY_BLOB} nctl:agdnsmasq\n")

    fingerprint = compute_sha256_fingerprint(KEY_BLOB)
    probe = _probe(keyscan_stdout=_keyscan_line())
    envelope = build_ssh_enroll(cfg, "agdnsmasq", fingerprints=[fingerprint], apply_changes=False, probe=probe)
    assert envelope.ok
    assert envelope.data.action == "enroll"  # not "noop": the bracketed entry does not count


def test_verified_reenrollment_removes_obsolete_bracketed_entry(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _patch_snapshot(monkeypatch, port=2222)
    alias = derive_host_key_alias(NODE_ID)
    known_hosts_path = cfg.resolved_ssh_known_hosts_file()
    known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
    known_hosts_path.write_text(
        "# preserved comment\n"
        f"[{alias}]:2222 ssh-ed25519 {KEY_BLOB} nctl:agdnsmasq\n"
        "nctl-node-00000000-0000-0000-0000-000000000099 ssh-ed25519 " + OTHER_KEY_BLOB + " nctl:other\n"
    )

    fingerprint = compute_sha256_fingerprint(KEY_BLOB)
    probe = _probe(keyscan_stdout=_keyscan_line())
    envelope = build_ssh_enroll(cfg, "agdnsmasq", fingerprints=[fingerprint], apply_changes=True, probe=probe)
    assert envelope.ok, envelope.errors

    content = known_hosts_path.read_text()
    assert f"[{alias}]:2222" not in content
    assert content.count(alias) == 1  # only the freshly written bare-alias entry
    assert "# preserved comment" in content
    assert "nctl-node-00000000-0000-0000-0000-000000000099" in content


def test_unverified_reenrollment_does_not_touch_obsolete_bracketed_entry(tmp_path, monkeypatch):
    # An unverified scan must perform no write at all -- including not
    # purging the obsolete bracketed entry.
    cfg = _config(tmp_path)
    _patch_snapshot(monkeypatch, port=2222)
    alias = derive_host_key_alias(NODE_ID)
    known_hosts_path = cfg.resolved_ssh_known_hosts_file()
    known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
    stale_content = f"[{alias}]:2222 ssh-ed25519 {KEY_BLOB} nctl:agdnsmasq\n"
    known_hosts_path.write_text(stale_content)

    probe = _probe(keyscan_stdout=_keyscan_line(OTHER_KEY_BLOB))
    envelope = build_ssh_enroll(cfg, "agdnsmasq", apply_changes=True, probe=probe)
    assert not envelope.ok
    assert envelope.errors[0].code == "host_key_unverified"
    assert known_hosts_path.read_text() == stale_content


def test_port_22_never_computes_an_obsolete_bracketed_name(tmp_path, monkeypatch):
    # At the default port there is no bracketed form to purge -- the bare
    # alias is already the only ever-correct managed name.
    from nctl_core.ssh_enroll import _obsolete_alias_port_lookup_name

    assert _obsolete_alias_port_lookup_name("nctl-node-x", 22) is None
    assert _obsolete_alias_port_lookup_name("nctl-node-x", 2222) == "[nctl-node-x]:2222"


def test_from_known_hosts_promotion_uses_port_aware_effective_config(tmp_path, monkeypatch):
    # find_legacy_trusted_keys must probe `ssh -G -p <port>` (not a portless
    # probe) and search the legacy store under the resulting lookup name.
    cfg = _config(tmp_path)
    _patch_snapshot(monkeypatch, port=2222)
    legacy_file = tmp_path / "known_hosts"
    legacy_line = f"[agdnsmasq.local]:2222 ssh-ed25519 {KEY_BLOB}"
    legacy_file.write_text(legacy_line + "\n")
    probe = _probe(keyscan_stdout=_keyscan_line(), legacy_lines=[legacy_line], known_hosts_files=[legacy_file])
    envelope = build_ssh_enroll(cfg, "agdnsmasq", from_known_hosts=True, apply_changes=True, probe=probe)
    assert envelope.ok, envelope.errors
    assert envelope.data.verified_source == "from_known_hosts"
    assert envelope.data.applied is True


def test_from_known_hosts_promotion_honors_effective_host_key_alias(tmp_path, monkeypatch):
    # When the developer's own ssh_config sets a HostKeyAlias for this host,
    # the legacy search must key off that alias (no port suffix), not
    # `[host]:port`.
    cfg = _config(tmp_path)
    _patch_snapshot(monkeypatch, port=2222)
    legacy_file = tmp_path / "known_hosts"
    legacy_line = f"my-custom-alias ssh-ed25519 {KEY_BLOB}"
    legacy_file.write_text(legacy_line + "\n")
    probe = _probe(
        keyscan_stdout=_keyscan_line(),
        legacy_lines=[legacy_line],
        known_hosts_files=[legacy_file],
        effective_host_key_alias="my-custom-alias",
    )
    envelope = build_ssh_enroll(cfg, "agdnsmasq", from_known_hosts=True, apply_changes=True, probe=probe)
    assert envelope.ok, envelope.errors
    assert envelope.data.verified_source == "from_known_hosts"


def test_read_only_managed_file_returns_structured_error(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _patch_snapshot(monkeypatch)
    known_hosts_path = cfg.resolved_ssh_known_hosts_file()
    known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
    known_hosts_path.write_text(f"nctl-node-other ssh-ed25519 {KEY_BLOB} nctl:other\n")
    known_hosts_path.chmod(0o000)
    try:
        fingerprint = compute_sha256_fingerprint(KEY_BLOB)
        probe = _probe(keyscan_stdout=_keyscan_line())
        envelope = build_ssh_enroll(cfg, "agdnsmasq", fingerprints=[fingerprint], apply_changes=True, probe=probe)
        assert not envelope.ok
        assert envelope.errors[0].code == "ssh_store_read_failed"
    finally:
        known_hosts_path.chmod(0o600)


def test_unwritable_directory_returns_structured_error(tmp_path, monkeypatch):
    # _atomic_write always coerces the destination's parent directory back to
    # 0o700 (it must, to create it on first use), so a plain chmod cannot
    # simulate a permission failure. Instead, make the parent path a regular
    # file: creating/using it as a directory then fails at the filesystem level.
    cfg = _config(tmp_path)
    _patch_snapshot(monkeypatch)
    known_hosts_path = cfg.resolved_ssh_known_hosts_file()
    known_hosts_path.parent.write_bytes(b"not a directory")

    fingerprint = compute_sha256_fingerprint(KEY_BLOB)
    probe = _probe(keyscan_stdout=_keyscan_line())
    envelope = build_ssh_enroll(cfg, "agdnsmasq", fingerprints=[fingerprint], apply_changes=True, probe=probe)
    assert not envelope.ok
    assert envelope.errors[0].code == "ssh_store_write_failed"


def test_nautobot_client_closed_on_success_and_error_paths(tmp_path, monkeypatch):
    closed = []

    class _TrackedClient:
        def close(self) -> None:
            closed.append(True)

    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()

    monkeypatch.setattr(ssh_enroll, "NautobotClient", lambda url, token: _TrackedClient())
    monkeypatch.setattr(ssh_enroll, "fetch_desired_snapshot", lambda client: _snapshot())
    build_ssh_enroll(cfg=_config(dir_a), host="does-not-exist", probe=_probe())
    assert closed == [True]

    closed.clear()
    monkeypatch.setattr(ssh_enroll, "fetch_desired_snapshot", lambda client: _snapshot())
    fingerprint = compute_sha256_fingerprint(KEY_BLOB)
    build_ssh_enroll(
        cfg=_config(dir_b),
        host="agdnsmasq",
        fingerprints=[fingerprint],
        apply_changes=True,
        probe=_probe(keyscan_stdout=_keyscan_line()),
    )
    assert closed == [True]


def test_idempotent_after_port_change_same_bare_alias_no_reenrollment_needed(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    fingerprint = compute_sha256_fingerprint(KEY_BLOB)

    _patch_snapshot(monkeypatch, port=None)
    probe = _probe(keyscan_stdout=_keyscan_line())
    first = build_ssh_enroll(cfg, "agdnsmasq", fingerprints=[fingerprint], apply_changes=True, probe=probe)
    assert first.ok and first.data.applied is True

    _patch_snapshot(monkeypatch, port=2222)
    second = build_ssh_enroll(cfg, "agdnsmasq", fingerprints=[fingerprint], apply_changes=True, probe=probe)
    assert second.ok
    assert second.data.action == "noop"
    assert second.data.applied is False


def test_keyscan_timeout_is_reported(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _patch_snapshot(monkeypatch)
    probe = _probe(keyscan_raises=subprocess.TimeoutExpired(cmd=["ssh-keyscan"], timeout=1))
    envelope = build_ssh_enroll(cfg, "agdnsmasq", probe=probe)
    assert not envelope.ok
    assert envelope.errors[0].code == "ssh_probe_failed"


def test_keyscan_missing_executable_is_reported(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _patch_snapshot(monkeypatch)
    probe = _probe(keyscan_raises=FileNotFoundError("ssh-keyscan not found"))
    envelope = build_ssh_enroll(cfg, "agdnsmasq", probe=probe)
    assert not envelope.ok
    assert envelope.errors[0].code == "ssh_probe_failed"


def test_legacy_probe_timeout_is_reported_as_structured_error(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _patch_snapshot(monkeypatch)
    probe = _probe(
        keyscan_stdout=_keyscan_line(),
        effective_config_raises=subprocess.TimeoutExpired(cmd=["ssh", "-G"], timeout=10),
    )
    envelope = build_ssh_enroll(cfg, "agdnsmasq", from_known_hosts=True, probe=probe)
    assert not envelope.ok
    assert envelope.errors[0].code == "ssh_probe_failed"


def test_legacy_probe_missing_executable_is_reported_as_structured_error(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _patch_snapshot(monkeypatch)
    probe = _probe(keyscan_stdout=_keyscan_line(), effective_config_raises=FileNotFoundError("ssh not found"))
    envelope = build_ssh_enroll(cfg, "agdnsmasq", from_known_hosts=True, probe=probe)
    assert not envelope.ok
    assert envelope.errors[0].code == "ssh_probe_failed"


def test_legacy_probe_nonzero_exit_is_reported_as_structured_error(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _patch_snapshot(monkeypatch)
    probe = _probe(keyscan_stdout=_keyscan_line(), effective_config_returncode=255)
    envelope = build_ssh_enroll(cfg, "agdnsmasq", from_known_hosts=True, probe=probe)
    assert not envelope.ok
    assert envelope.errors[0].code == "ssh_probe_failed"


def test_malformed_keyscan_output_is_rejected(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _patch_snapshot(monkeypatch)
    probe = _probe(keyscan_stdout="agdnsmasq.local not-a-real-keytype " + KEY_BLOB + "\n")
    fingerprint = compute_sha256_fingerprint(KEY_BLOB)
    envelope = build_ssh_enroll(cfg, "agdnsmasq", fingerprints=[fingerprint], probe=probe)
    assert not envelope.ok
    assert envelope.errors[0].code == "ssh_probe_failed"


def test_json_envelope_never_includes_raw_key_blob(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _patch_snapshot(monkeypatch)
    fingerprint = compute_sha256_fingerprint(KEY_BLOB)
    probe = _probe(keyscan_stdout=_keyscan_line())
    envelope = build_ssh_enroll(cfg, "agdnsmasq", fingerprints=[fingerprint], apply_changes=True, probe=probe)
    payload = envelope.to_json()
    assert KEY_BLOB not in payload
    assert '"schema": "' + SSH_ENROLL_SCHEMA + '"' in payload


# fix_sshkey4 Step 1: the one strict managed known_hosts store reader.

ALIAS = "nctl-node-27818c12-fe15-4c9f-83d0-7949523f6c33"
OTHER_ALIAS = "nctl-node-00000000-0000-0000-0000-000000000099"


def test_absent_store_is_empty_not_a_read_failure(tmp_path):
    store = load_managed_ssh_store(tmp_path / "known_hosts")
    assert store.raw_lines == ()
    assert store.entries == ()
    assert store.obsolete_entries == ()
    assert store.entries_for(ALIAS) == []


def test_valid_comments_and_multiple_aliases_remain_readable(tmp_path):
    path = tmp_path / "known_hosts"
    path.write_text(
        "# a comment\n"
        "\n"
        f"{ALIAS} ssh-ed25519 {KEY_BLOB} nctl:agdnsmasq\n"
        f"{OTHER_ALIAS} ssh-rsa {OTHER_KEY_BLOB} nctl:other\n"
    )
    store = load_managed_ssh_store(path)
    assert len(store.entries) == 2
    assert store.entries_for(ALIAS)[0].key_type == "ssh-ed25519"
    assert store.entries_for(OTHER_ALIAS)[0].key_type == "ssh-rsa"


def test_valid_obsolete_bracketed_entry_is_recognized_separately(tmp_path):
    path = tmp_path / "known_hosts"
    path.write_text(f"[{ALIAS}]:2222 ssh-ed25519 {KEY_BLOB} nctl:agdnsmasq\n")
    store = load_managed_ssh_store(path)
    assert store.entries == ()
    assert len(store.obsolete_entries) == 1
    obsolete = store.obsolete_entries[0]
    assert obsolete.alias == ALIAS
    assert obsolete.port == 2222
    assert store.entries_for(ALIAS) == []  # never satisfies current enrollment


@pytest.mark.parametrize(
    "bad_line",
    [
        f"{ALIAS} ssh-rsa\n",  # malformed field count
        f"{ALIAS} not-a-real-keytype {KEY_BLOB}\n",  # unknown key type
        f"{ALIAS} ssh-ed25519 not-valid-base64!!!\n",  # invalid base64
        f"@cert-authority {ALIAS} ssh-ed25519 {KEY_BLOB}\n",  # marker entry
        f"|1|abcdefghijklmnopqrstuvwxyz1234==|abcdefghijklmnopqrstuvwxyz5678== ssh-ed25519 {KEY_BLOB}\n",  # hashed name
        f"agdnsmasq.local ssh-ed25519 {KEY_BLOB}\n",  # endpoint-keyed name, not a bare alias
        f"[{ALIAS}]:99999 ssh-ed25519 {KEY_BLOB}\n",  # out-of-range obsolete port
        f"{ALIAS},agdnsmasq.local ssh-ed25519 {KEY_BLOB}\n",  # more than one name
    ],
)
def test_malformed_or_unsupported_line_is_a_structured_store_failure(tmp_path, bad_line):
    path = tmp_path / "known_hosts"
    path.write_text(bad_line)
    with pytest.raises(SshStoreReadError):
        load_managed_ssh_store(path)


def test_invalid_utf8_store_is_a_structured_store_failure(tmp_path):
    path = tmp_path / "known_hosts"
    path.write_bytes(b"\xff\xfe not valid utf-8\n")
    with pytest.raises(SshStoreReadError):
        load_managed_ssh_store(path)


def test_one_malformed_unrelated_line_fails_the_whole_store(tmp_path):
    path = tmp_path / "known_hosts"
    path.write_text(
        f"{ALIAS} ssh-ed25519 {KEY_BLOB} nctl:agdnsmasq\n"
        "some.endpoint.local ssh-ed25519 " + OTHER_KEY_BLOB + "\n"
    )
    with pytest.raises(SshStoreReadError):
        load_managed_ssh_store(path)


def test_corrupt_store_prevents_enrollment_and_preserves_original_bytes(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _patch_snapshot(monkeypatch)
    known_hosts_path = cfg.resolved_ssh_known_hosts_file()
    known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
    corrupt_content = "agdnsmasq.local ssh-ed25519 " + KEY_BLOB + "\n"
    known_hosts_path.write_text(corrupt_content)

    fingerprint = compute_sha256_fingerprint(KEY_BLOB)
    probe = _probe(keyscan_stdout=_keyscan_line())
    envelope = build_ssh_enroll(cfg, "agdnsmasq", fingerprints=[fingerprint], apply_changes=True, probe=probe)
    assert not envelope.ok
    assert envelope.errors[0].code == "ssh_store_read_failed"
    assert known_hosts_path.read_text() == corrupt_content
