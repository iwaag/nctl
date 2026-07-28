"""Shared reconcile SSH scan-error policy contracts."""

from nctl_core.reconcile.ssh_preflight import (
    STATUS_UNENROLLED,
    SshPreflightEntry,
    ssh_scan_errors,
)


def test_ssh_scan_errors_maps_unenrolled_status_too():
    entries = [SshPreflightEntry(slug="agdnsmasq", alias="nctl-node-x", status=STATUS_UNENROLLED)]
    errors = ssh_scan_errors(entries)
    assert len(errors) == 1
    assert errors[0].code == "ssh_host_key_unenrolled"
    assert "agdnsmasq" in errors[0].message
