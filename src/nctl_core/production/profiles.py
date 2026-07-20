"""Read the ansible_agdev-owned deployment_profiles map (Phase 2 Step 2).

Deployment profiles are read directly from the ansible_agdev checkout's
`vars/deployment_profiles.yml` instead of through nintent's Job-input byte
contract (`parse_profile_job_input`, and the `verify_deployment_profiles_contract.yml`
/ `nintent_serialize_deployment_profiles.yml` playbook handshake that fed it).
That contract existed only because composition ran inside Nautobot while the
profiles lived in ansible_agdev; now that composition runs in nctl, which can
read the ansible_agdev checkout directly (see `[ansible] playbook_dir` in
`nctl.toml`), the transport half of the contract has no reason to exist.

`canonical_json_digest` is still computed locally so the production schema
document/report's `deployment_profile_digest` field is unchanged — it is
provenance (a fingerprint of which profile revision produced this inventory)
rather than a verified handshake between two processes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .contract import ContractError, canonical_json_digest, validate_deployment_profiles

DEPLOYMENT_PROFILES_PATH = Path("vars/deployment_profiles.yml")


class DeploymentProfilesError(Exception):
    """The deployment_profiles file is missing, unparsable, or fails validation."""


def load_deployment_profiles(playbook_dir: Path) -> tuple[dict[str, Any], str]:
    """Return the validated profile map and its canonical-JSON digest."""

    path = playbook_dir / DEPLOYMENT_PROFILES_PATH
    try:
        text = path.read_text()
    except OSError as exc:
        raise DeploymentProfilesError(f"cannot read {path}: {exc}") from exc

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise DeploymentProfilesError(f"cannot parse {path}: {exc}") from exc

    if not isinstance(raw, dict) or "deployment_profiles" not in raw:
        raise DeploymentProfilesError(f"{path} must define a top-level deployment_profiles mapping")

    try:
        profiles = validate_deployment_profiles(raw["deployment_profiles"])
    except ContractError as exc:
        raise DeploymentProfilesError(f"{path}: {exc}") from exc

    return profiles, canonical_json_digest(profiles)
