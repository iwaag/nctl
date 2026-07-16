from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from nctl_core.reconcile.profiles import ProfileReconciliationError, load_profile_reconciliation

_REPO_PROFILE_NAMES = {
    "dnsmasq",
    "grafana",
    "home_assistant",
    "nomad_client",
    "nomad_server",
    "prometheus",
    "prometheus_node_exporter",
}


def _write(tmp_path: Path, body: dict) -> Path:
    playbook_dir = tmp_path / "ansible_agdev"
    (playbook_dir / "vars").mkdir(parents=True)
    (playbook_dir / "vars" / "deployment_profiles.yml").write_text(yaml.safe_dump(body))
    (playbook_dir / "playbooks" / "monitoring").mkdir(parents=True)
    (playbook_dir / "playbooks" / "monitoring" / "setup_grafana.yml").write_text("- hosts: all\n")
    return playbook_dir


def test_real_repo_file_validates(tmp_path):
    # The actual checked-in file this Step 5 boundary edits -- a real
    # regression gate, not just a synthetic fixture.
    repo_playbook_dir = Path(__file__).resolve().parents[2] / "ansible_agdev"
    entries = load_profile_reconciliation(repo_playbook_dir, _REPO_PROFILE_NAMES)

    assert entries["dnsmasq"].action.kind == "dnsmasq_config"
    assert entries["home_assistant"].observe_only is True
    assert entries["nomad_client"].dependencies == ["nomad_server"]
    assert entries["prometheus_node_exporter"].dependencies == ["prometheus"]
    assert entries["nomad_server"].action.kind == "playbook"
    assert entries["nomad_client"].action.playbook_by_os == {
        "linux": "playbooks/nomad/setup_nomad_client.yml",
        "macos": "playbooks/nomad/setup_nomad_client_macos.yml",
    }


def test_profile_absent_from_reconciliation_is_simply_not_present(tmp_path):
    playbook_dir = _write(
        tmp_path,
        {
            "deployment_profiles": {},
            "deployment_profile_reconciliation": {
                "grafana": {"action": {"kind": "playbook", "playbook": "playbooks/monitoring/setup_grafana.yml"}},
            },
        },
    )

    entries = load_profile_reconciliation(playbook_dir, {"grafana", "prometheus"})

    assert set(entries) == {"grafana"}


def test_unknown_profile_name_is_rejected(tmp_path):
    playbook_dir = _write(
        tmp_path,
        {"deployment_profile_reconciliation": {"nope": {"observe_only": True}}},
    )

    with pytest.raises(ProfileReconciliationError, match="unknown profiles"):
        load_profile_reconciliation(playbook_dir, {"grafana"})


def test_unknown_dependency_is_rejected(tmp_path):
    playbook_dir = _write(
        tmp_path,
        {
            "deployment_profile_reconciliation": {
                "grafana": {
                    "action": {"kind": "playbook", "playbook": "playbooks/monitoring/setup_grafana.yml"},
                    "dependencies": ["ghost"],
                }
            }
        },
    )

    with pytest.raises(ProfileReconciliationError, match="unknown profiles"):
        load_profile_reconciliation(playbook_dir, {"grafana"})


def test_dependency_cycle_is_rejected(tmp_path):
    playbook_dir = _write(
        tmp_path,
        {
            "deployment_profile_reconciliation": {
                "a": {"action": {"kind": "dnsmasq_config"}, "dependencies": ["b"]},
                "b": {"action": {"kind": "dnsmasq_config"}, "dependencies": ["a"]},
            }
        },
    )

    with pytest.raises(ProfileReconciliationError, match="cycle"):
        load_profile_reconciliation(playbook_dir, {"a", "b"})


def test_action_and_observe_only_are_mutually_exclusive(tmp_path):
    playbook_dir = _write(
        tmp_path,
        {
            "deployment_profile_reconciliation": {
                "grafana": {
                    "action": {"kind": "dnsmasq_config"},
                    "observe_only": True,
                }
            }
        },
    )

    with pytest.raises(ProfileReconciliationError):
        load_profile_reconciliation(playbook_dir, {"grafana"})


def test_entry_with_neither_action_nor_observe_only_is_rejected(tmp_path):
    playbook_dir = _write(tmp_path, {"deployment_profile_reconciliation": {"grafana": {}}})

    with pytest.raises(ProfileReconciliationError):
        load_profile_reconciliation(playbook_dir, {"grafana"})


def test_playbook_path_must_be_relative(tmp_path):
    playbook_dir = _write(
        tmp_path,
        {
            "deployment_profile_reconciliation": {
                "grafana": {"action": {"kind": "playbook", "playbook": "/etc/passwd"}},
            }
        },
    )

    with pytest.raises(ProfileReconciliationError, match="must be relative"):
        load_profile_reconciliation(playbook_dir, {"grafana"})


def test_playbook_path_cannot_escape_playbook_dir(tmp_path):
    playbook_dir = _write(
        tmp_path,
        {
            "deployment_profile_reconciliation": {
                "grafana": {"action": {"kind": "playbook", "playbook": "../../etc/passwd"}},
            }
        },
    )

    with pytest.raises(ProfileReconciliationError, match="escapes"):
        load_profile_reconciliation(playbook_dir, {"grafana"})


def test_dnsmasq_config_action_forbids_playbook_field(tmp_path):
    playbook_dir = _write(
        tmp_path,
        {
            "deployment_profile_reconciliation": {
                "dnsmasq": {"action": {"kind": "dnsmasq_config", "playbook": "playbooks/x.yml"}},
            }
        },
    )

    with pytest.raises(ProfileReconciliationError):
        load_profile_reconciliation(playbook_dir, {"dnsmasq"})


def test_playbook_action_needs_exactly_one_of_playbook_or_playbook_by_os(tmp_path):
    playbook_dir = _write(
        tmp_path,
        {"deployment_profile_reconciliation": {"grafana": {"action": {"kind": "playbook"}}}},
    )

    with pytest.raises(ProfileReconciliationError):
        load_profile_reconciliation(playbook_dir, {"grafana"})
