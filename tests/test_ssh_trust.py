import base64
import hashlib
import shlex

import pytest

from nctl_core.ssh_trust import (
    ManagedEntry,
    ParsedHostKeyLine,
    SshTrustError,
    build_ansible_ssh_common_args,
    compute_sha256_fingerprint,
    derive_host_key_alias,
    derive_lookup_name,
    find_managed_entry,
    is_hashed_hostname_entry,
    parse_known_hosts_line,
    validate_desired_node_id,
)

NODE_ID = "27818c12-fe15-4c9f-83d0-7949523f6c33"
OTHER_NODE_ID = "00000000-0000-0000-0000-000000000001"


def make_key_blob(payload: bytes = b"fake-ed25519-key-bytes") -> str:
    return base64.b64encode(payload).decode("ascii")


def test_validate_desired_node_id_accepts_canonical_uuid():
    assert validate_desired_node_id(NODE_ID) == NODE_ID


def test_validate_desired_node_id_accepts_uppercase_and_normalizes():
    assert validate_desired_node_id(NODE_ID.upper()) == NODE_ID


@pytest.mark.parametrize("bad", ["", "not-a-uuid", "agdnsmasq.local", "192.168.0.2", "27818c12fe154c9f83d07949523f6c33"])
def test_validate_desired_node_id_rejects_non_uuid(bad):
    with pytest.raises(SshTrustError):
        validate_desired_node_id(bad)


def test_derive_host_key_alias_is_deterministic():
    assert derive_host_key_alias(NODE_ID) == f"nctl-node-{NODE_ID}"
    assert derive_host_key_alias(NODE_ID) == derive_host_key_alias(NODE_ID)


def test_derive_host_key_alias_differs_for_different_node_ids():
    assert derive_host_key_alias(NODE_ID) != derive_host_key_alias(OTHER_NODE_ID)


def test_derive_host_key_alias_contains_no_endpoint_name():
    alias = derive_host_key_alias(NODE_ID)
    for forbidden in (".local", ".home.arpa", "192.168", "agdnsmasq"):
        assert forbidden not in alias


def test_derive_lookup_name_default_port_is_bare_alias():
    alias = derive_host_key_alias(NODE_ID)
    assert derive_lookup_name(alias) == alias
    assert derive_lookup_name(alias, port=22) == alias


def test_derive_lookup_name_non_default_port_is_bracketed():
    alias = derive_host_key_alias(NODE_ID)
    assert derive_lookup_name(alias, port=2222) == f"[{alias}]:2222"


def test_derive_lookup_name_rejects_invalid_port():
    alias = derive_host_key_alias(NODE_ID)
    with pytest.raises(SshTrustError):
        derive_lookup_name(alias, port=0)
    with pytest.raises(SshTrustError):
        derive_lookup_name(alias, port=70000)


def test_build_ansible_ssh_common_args_contains_strict_options():
    alias = derive_host_key_alias(NODE_ID)
    args = build_ansible_ssh_common_args(alias, "/home/user/.local/state/nctl/ssh/known_hosts")
    assert f"HostKeyAlias={alias}" in args
    assert "UserKnownHostsFile=/home/user/.local/state/nctl/ssh/known_hosts" in args
    assert "StrictHostKeyChecking=yes" in args
    assert "CheckHostIP=no" in args
    assert "UpdateHostKeys=no" in args


def test_build_ansible_ssh_common_args_quotes_paths_with_spaces():
    alias = derive_host_key_alias(NODE_ID)
    args = build_ansible_ssh_common_args(alias, "/path with spaces/known_hosts")
    assert "UserKnownHostsFile=/path with spaces/known_hosts" in args
    assert shlex.quote("UserKnownHostsFile=/path with spaces/known_hosts") in args


def test_build_ansible_ssh_common_args_rejects_empty_inputs():
    with pytest.raises(SshTrustError):
        build_ansible_ssh_common_args("", "/known_hosts")
    with pytest.raises(SshTrustError):
        build_ansible_ssh_common_args("alias", "")


def test_compute_sha256_fingerprint_matches_known_vector():
    payload = b"deterministic-key-bytes-for-test"
    blob_b64 = base64.b64encode(payload).decode("ascii")
    expected = "SHA256:" + base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii").rstrip("=")
    assert compute_sha256_fingerprint(blob_b64) == expected


def test_compute_sha256_fingerprint_rejects_malformed_base64():
    with pytest.raises(SshTrustError):
        compute_sha256_fingerprint("not-valid-base64!!!")


def test_compute_sha256_fingerprint_rejects_empty_blob():
    with pytest.raises(SshTrustError):
        compute_sha256_fingerprint("")


def test_parse_known_hosts_line_ordinary_entry():
    blob = make_key_blob()
    line = f"agdnsmasq.local ssh-ed25519 {blob} root@agdnsmasq"
    parsed = parse_known_hosts_line(line)
    assert parsed == ParsedHostKeyLine(
        names=("agdnsmasq.local",),
        key_type="ssh-ed25519",
        key_blob_b64=blob,
        comment="root@agdnsmasq",
    )


def test_parse_known_hosts_line_multiple_hostnames():
    blob = make_key_blob()
    parsed = parse_known_hosts_line(f"agdnsmasq.local,192.168.0.2 ssh-ed25519 {blob}")
    assert parsed.names == ("agdnsmasq.local", "192.168.0.2")
    assert parsed.comment is None


def test_parse_known_hosts_line_skips_blank_and_comments():
    assert parse_known_hosts_line("") is None
    assert parse_known_hosts_line("   ") is None
    assert parse_known_hosts_line("# a comment") is None


def test_parse_known_hosts_line_skips_markers():
    blob = make_key_blob()
    assert parse_known_hosts_line(f"@cert-authority * ssh-ed25519 {blob}") is None
    assert parse_known_hosts_line(f"@revoked agdnsmasq.local ssh-ed25519 {blob}") is None


def test_parse_known_hosts_line_rejects_malformed_line():
    with pytest.raises(SshTrustError):
        parse_known_hosts_line("agdnsmasq.local ssh-ed25519")


def test_parse_known_hosts_line_rejects_unknown_key_type():
    blob = make_key_blob()
    with pytest.raises(SshTrustError):
        parse_known_hosts_line(f"agdnsmasq.local not-a-real-keytype {blob}")


def test_parse_known_hosts_line_rejects_malformed_base64():
    with pytest.raises(SshTrustError):
        parse_known_hosts_line("agdnsmasq.local ssh-ed25519 not-valid-base64!!!")


def test_is_hashed_hostname_entry():
    assert is_hashed_hostname_entry("|1|abcd1234==|efgh5678==")
    assert not is_hashed_hostname_entry("agdnsmasq.local")


def test_find_managed_entry_matches_by_alias_only():
    alias = derive_host_key_alias(NODE_ID)
    blob = make_key_blob()
    entries = [ManagedEntry(alias=alias, key_type="ssh-ed25519", key_blob_b64=blob)]
    assert find_managed_entry(entries, alias) == entries[0]
    assert find_managed_entry(entries, alias, key_type="ssh-ed25519") == entries[0]
    assert find_managed_entry(entries, alias, key_type="ssh-rsa") is None


def test_find_managed_entry_no_match_for_other_alias():
    alias = derive_host_key_alias(NODE_ID)
    other_alias = derive_host_key_alias(OTHER_NODE_ID)
    blob = make_key_blob()
    entries = [ManagedEntry(alias=alias, key_type="ssh-ed25519", key_blob_b64=blob)]
    assert find_managed_entry(entries, other_alias) is None


def test_duplicate_managed_entries_return_first_match():
    alias = derive_host_key_alias(NODE_ID)
    blob_a = make_key_blob(b"key-a")
    blob_b = make_key_blob(b"key-b")
    entries = [
        ManagedEntry(alias=alias, key_type="ssh-ed25519", key_blob_b64=blob_a),
        ManagedEntry(alias=alias, key_type="ssh-ed25519", key_blob_b64=blob_b),
    ]
    assert find_managed_entry(entries, alias).key_blob_b64 == blob_a
