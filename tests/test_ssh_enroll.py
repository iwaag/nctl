from __future__ import annotations

import subprocess
from pathlib import Path

import nctl_core.ssh_enroll as ssh_enroll
from nctl_core.config import Config
from nctl_core.ssh_enroll import (
    SSH_ENROLL_SCHEMA,
    SshProbeRunner,
    build_ssh_enroll,
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


def _patch_snapshot(monkeypatch, port: int | None = None) -> None:
    monkeypatch.setattr(ssh_enroll, "NautobotClient", lambda url, token: object())
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
) -> SshProbeRunner:
    def keyscan(host: str, port: int, timeout: float) -> subprocess.CompletedProcess[str]:
        if keyscan_raises is not None:
            raise keyscan_raises
        return subprocess.CompletedProcess(args=["ssh-keyscan"], returncode=keyscan_returncode, stdout=keyscan_stdout, stderr="")

    def known_hosts_files_for(host: str) -> list[Path]:
        return known_hosts_files or []

    def keygen_find(path: Path, hostname: str) -> subprocess.CompletedProcess[str]:
        stdout = "\n".join(legacy_lines or []) + ("\n" if legacy_lines else "")
        return subprocess.CompletedProcess(args=["ssh-keygen"], returncode=0, stdout=stdout, stderr="")

    return SshProbeRunner(keyscan=keyscan, known_hosts_files_for=known_hosts_files_for, keygen_find=keygen_find)


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
    assert not cfg.ssh.resolved_known_hosts_file().exists()


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
    content = cfg.ssh.resolved_known_hosts_file().read_text()
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
    content = cfg.ssh.resolved_known_hosts_file().read_text()
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

    known_hosts_path = cfg.ssh.resolved_known_hosts_file()
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
    assert not cfg.ssh.resolved_known_hosts_file().exists()


def test_non_default_port_is_used_in_lookup_name(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _patch_snapshot(monkeypatch, port=2222)
    fingerprint = compute_sha256_fingerprint(KEY_BLOB)
    probe = _probe(keyscan_stdout=_keyscan_line())
    envelope = build_ssh_enroll(cfg, "agdnsmasq", fingerprints=[fingerprint], apply_changes=False, probe=probe)
    assert envelope.data.port == 2222
    assert envelope.data.lookup_name == f"[{envelope.data.alias}]:2222"


def test_keyscan_timeout_is_reported(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    _patch_snapshot(monkeypatch)
    probe = _probe(keyscan_raises=subprocess.TimeoutExpired(cmd=["ssh-keyscan"], timeout=1))
    envelope = build_ssh_enroll(cfg, "agdnsmasq", probe=probe)
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
