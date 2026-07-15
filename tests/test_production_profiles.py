from __future__ import annotations

from pathlib import Path

import pytest

from nctl_core.production.contract import canonical_json_digest
from nctl_core.production.profiles import DeploymentProfilesError, load_deployment_profiles

VALID_YAML = """
deployment_profiles:
  dnsmasq:
    group: dnsmasq_server
    config_schema_version: "1"
    variables:
      enable_dhcp:
        ansible_variable: dnsmasq_enable_dhcp
        type: boolean
        required: false
"""


def write_profiles(tmp_path: Path, text: str) -> Path:
    playbook_dir = tmp_path / "ansible_agdev"
    vars_dir = playbook_dir / "vars"
    vars_dir.mkdir(parents=True)
    (vars_dir / "deployment_profiles.yml").write_text(text)
    return playbook_dir


def test_load_deployment_profiles_returns_validated_map_and_digest(tmp_path):
    playbook_dir = write_profiles(tmp_path, VALID_YAML)

    profiles, digest = load_deployment_profiles(playbook_dir)

    assert profiles["dnsmasq"]["group"] == "dnsmasq_server"
    assert digest == canonical_json_digest(profiles)


def test_load_deployment_profiles_missing_file(tmp_path):
    playbook_dir = tmp_path / "ansible_agdev"
    playbook_dir.mkdir()
    with pytest.raises(DeploymentProfilesError, match="cannot read"):
        load_deployment_profiles(playbook_dir)


def test_load_deployment_profiles_missing_top_level_key(tmp_path):
    playbook_dir = write_profiles(tmp_path, "not_the_right_key: {}\n")
    with pytest.raises(DeploymentProfilesError, match="deployment_profiles mapping"):
        load_deployment_profiles(playbook_dir)


def test_load_deployment_profiles_invalid_profile_shape(tmp_path):
    playbook_dir = write_profiles(
        tmp_path,
        "deployment_profiles:\n  dnsmasq:\n    group: 'Not A Slug!'\n    config_schema_version: '1'\n    variables: {}\n",
    )
    with pytest.raises(DeploymentProfilesError, match="invalid_slug"):
        load_deployment_profiles(playbook_dir)
