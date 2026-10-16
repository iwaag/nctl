"""project_migration p6: the nfs_server / nfs_client profiles and their state proofs.

A shared area is configured, running, mounted and usable as separate facts.
The real repository metadata must render the config-resolved `nfs_export` /
`nfs_mount` hints, and drift must read anything short of `present` as
`service_missing` -- a declaration or a configured fstab line alone is not a
usable share.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from nctl_core.drift.service_placement import evaluate_placement_drift
from nctl_core.observation import render_probe_hints
from nctl_core.production.profiles import load_deployment_profiles
from nctl_core.reconcile.profiles import (
    CheckResolutionError,
    ProfileReconciliation,
    ProfileReconciliationError,
    load_profile_reconciliation,
    resolve_check_hints,
)
from nctl_core.sources.desired import DesiredNode, DesiredService, DesiredServicePlacement, DesiredSnapshot

NOW = datetime(2026, 10, 16, 0, 0, tzinfo=timezone.utc)
NODE_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, "nctl-test-node:client"))
MOUNT = "/mnt/share"


def _real_reconciliation():
    playbook_dir = Path(__file__).resolve().parents[2] / "ansible_agdev"
    profiles, _digest = load_deployment_profiles(playbook_dir)
    return profiles, load_profile_reconciliation(playbook_dir, set(profiles))


def test_repo_declares_nfs_profiles_with_state_proofs():
    profiles, entries = _real_reconciliation()

    assert profiles["nfs_server"]["placement_list_variable"] == "nfs_server_exports"
    assert profiles["nfs_client"]["placement_list_variable"] == "nfs_client_mounts"
    assert entries["nfs_server"].action.playbook_by_os == {
        "linux": "playbooks/services/setup_nfs_server.yml",
        "macos": "playbooks/services/setup_nfs_server_macos.yml",
    }
    assert entries["nfs_client"].action.playbook_by_os == {
        "linux": "playbooks/services/setup_nfs_client.yml",
        "macos": "playbooks/services/setup_nfs_client_macos.yml",
    }
    assert [(c.kind, c.path_from_config) for c in entries["nfs_server"].checks] == [("nfs_export", "path")]
    assert [(c.kind, c.path_from_config) for c in entries["nfs_client"].checks] == [("nfs_mount", "path")]
    assert entries["nfs_client"].dependencies == ["nfs_server"]


def test_nfs_checks_resolve_and_share_path_rules():
    entry = ProfileReconciliation.model_validate(
        {"observe_only": True, "checks": [{"kind": "nfs_mount", "path_from_config": "path"}]}
    )
    assert resolve_check_hints(entry, {"path": MOUNT}, "ctx") == [{"kind": "nfs_mount", "path": MOUNT}]
    for bad in ({}, {"path": ""}, {"path": "relative/x"}):
        with pytest.raises(CheckResolutionError, match="nfs_mount"):
            resolve_check_hints(entry, bad, "ctx")


def test_nfs_check_needs_exactly_one_path_source(tmp_path):
    playbook_dir = tmp_path / "ansible_agdev"
    (playbook_dir / "vars").mkdir(parents=True)
    (playbook_dir / "vars" / "deployment_profiles.yml").write_text(yaml.safe_dump({
        "deployment_profile_reconciliation": {"nfs_server": {"observe_only": True, "checks": [{"kind": "nfs_export"}]}}
    }))
    with pytest.raises(ProfileReconciliationError, match="exactly one of path or path_from_config"):
        load_profile_reconciliation(playbook_dir, {"nfs_server"})


def _desired() -> DesiredSnapshot:
    node = DesiredNode(id=NODE_ID, slug="client", name="client", lifecycle="active", node_type="device",
                       realized_device_id="d1")
    service = DesiredService(id="svc-mnt", slug="share-mount", name="share-mount", lifecycle="active")
    placement = DesiredServicePlacement(
        id="p1", service_id="svc-mnt", node_id=NODE_ID, instance_name="share-client",
        deployment_profile="nfs_client", config_schema_version="1",
        config={"share": "share", "server": "server.example", "export": "/srv/share", "path": MOUNT},
    )
    return DesiredSnapshot(nodes=[node], services=[service], placements=[placement])


def _evaluate(observed_services: dict) -> dict:
    devices = {"d1": {"observed_system": "Linux", "observed_services": observed_services,
                      "service_inventory_updated_at": "2026-10-15T23:00:00+00:00"}}
    return evaluate_placement_drift(
        [{"id": "svc-mnt", "name": "share-mount"}],
        [{"placement_id": "p1", "service_id": "svc-mnt", "node_id": NODE_ID, "node_slug": "client",
          "instance_name": "share-client", "deployment_profile": "nfs_client", "management_mode": "nctl_managed",
          "realized_device_id": "d1", "host_os": "linux"}],
        devices, {"d1": NODE_ID}, now=NOW, stale_after_hours=24,
    )["svc-mnt"]


@pytest.mark.parametrize("status", ["missing", "unmounted", "unreachable", "unusable"])
def test_anything_short_of_a_usable_mount_is_service_missing(status):
    _profiles, reconciliation = _real_reconciliation()
    hints = yaml.safe_load(render_probe_hints(_desired(), NODE_ID, reconciliation))
    assert hints["service_probe_hints"]["share-mount"]["checks"] == [{"kind": "nfs_mount", "path": MOUNT}]

    report = _evaluate({"share-mount": {
        "state": "missing", "source": "check:nfs_mount", "checked_at": "2026-10-15T23:00:00+00:00",
        "checks": [{"kind": "nfs_mount", "path": MOUNT, "status": status, "configured": status != "missing"}],
    }})
    assert [gap["code"] for gap in report["placements"][0]["gaps"]] == ["service_missing"]


def test_a_usable_mount_converges():
    report = _evaluate({"share-mount": {
        "state": "present", "source": "check:nfs_mount", "checked_at": "2026-10-15T23:00:00+00:00",
        "checks": [{"kind": "nfs_mount", "path": MOUNT, "status": "present", "configured": True,
                    "mounted": True, "access": "listable"}],
    }})
    assert report["placements"][0]["gaps"] == []
