"""Frozen executable contract for Phase 4 Decision 3 (p4/plan.md Step 4.1).

Written before implementation: pins that a missing/unparsable/invalid
`vars/deployment_profiles.yml` becomes a classified global ERROR
`deployment_profiles_unavailable` instead of silently degrading to `{}`
(current behavior documented in `drift_render.py`'s module docstring and
`comparators.py`'s `"if not context.profiles: return"` guard). Currently
failing because that degrade-to-`{}` behavior is still in place.
"""

import httpx
import respx

from nctl_core.config import Config
from nctl_core.drift.model import Severity
from nctl_core.drift_render import build_drift

BASE_URL = "http://nautobot.test"

EMPTY_DESIRED_RESPONSE = {
    "data": {
        "desired_nodes": [],
        "desired_endpoints": [],
        "desired_ip_ranges": [],
        "desired_node_operational_overrides": [],
        "desired_service_placements": [],
        "desired_services": [],
        "desired_dependencies": [],
    }
}


def make_config(tmp_path) -> Config:
    (tmp_path / "dumps").mkdir()
    config_path = tmp_path / "nctl.toml"
    config_path.write_text(
        f"""
[nautobot]
url = "{BASE_URL}"

[inventory]
dumps_dir = "{tmp_path / 'dumps'}"

[ansible]
playbook_dir = "{tmp_path / 'ansible_agdev'}"
inventory = "inventories/generated/hosts_intent.yml"
"""
    )
    return Config.load(config_path)


@respx.mock
def test_missing_deployment_profiles_becomes_global_error(tmp_path):
    # No vars/deployment_profiles.yml under playbook_dir at all.
    respx.post(f"{BASE_URL}/api/graphql/").mock(
        side_effect=[
            httpx.Response(200, json=EMPTY_DESIRED_RESPONSE),
            httpx.Response(200, json={"data": {"devices": [], "virtual_machines": [], "interfaces": [], "ip_addresses": []}}),
        ]
    )
    cfg = make_config(tmp_path)

    envelope = build_drift(cfg)

    assert envelope.ok is True
    global_targets = [t for t in envelope.data.targets if t.target.kind == "global"]
    assert global_targets, "expected a global target carrying deployment_profiles_unavailable"
    codes = [d.code for t in global_targets for d in t.diffs]
    assert "deployment_profiles_unavailable" in codes, f"expected global blocker code, got {codes}"
    severities = [d.severity for t in global_targets for d in t.diffs if d.code == "deployment_profiles_unavailable"]
    assert all(s == Severity.ERROR for s in severities)


@respx.mock
def test_invalid_deployment_profiles_becomes_global_error(tmp_path):
    ansible_dir = tmp_path / "ansible_agdev"
    (ansible_dir / "vars").mkdir(parents=True)
    (ansible_dir / "vars" / "deployment_profiles.yml").write_text("not_the_right_top_level_key: {}\n")

    respx.post(f"{BASE_URL}/api/graphql/").mock(
        side_effect=[
            httpx.Response(200, json=EMPTY_DESIRED_RESPONSE),
            httpx.Response(200, json={"data": {"devices": [], "virtual_machines": [], "interfaces": [], "ip_addresses": []}}),
        ]
    )
    cfg = make_config(tmp_path)

    envelope = build_drift(cfg)

    assert envelope.ok is True
    global_targets = [t for t in envelope.data.targets if t.target.kind == "global"]
    codes = [d.code for t in global_targets for d in t.diffs]
    assert "deployment_profiles_unavailable" in codes, f"expected global blocker code, got {codes}"
