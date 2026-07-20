"""Frozen executable contract for Phase 4 Decision 2 (p4/plan.md Step 4.1).

Written before implementation: pins that `production_policy` renames its
per-node INFO diagnostic from `derived_value_provenance` to
`intent_effect_summary`, with no old-code alias left behind. Currently
failing because `comparators.py` still emits the old code
(`nctl_core/drift/comparators.py:320`).
"""

import httpx
import respx

from nctl_core.config import Config
from nctl_core.drift_render import build_drift

BASE_URL = "http://nautobot.test"

ONE_NODE_DESIRED_RESPONSE = {
    "data": {
        "desired_nodes": [
            {
                "id": "node-1",
                "slug": "agok",
                "name": "agok",
                "lifecycle": "ACTIVE",
                "node_type": "DEVICE",
                "role": None,
                "accepted_actual_types": ["DEVICE"],
                "expected_spec": {},
                "realized_device": None,
                "realized_device_source": None,
                "realized_vm": None,
                "realized_vm_source": None,
            }
        ],
        "desired_endpoints": [],
        "desired_ip_ranges": [],
        "desired_node_operational_overrides": [],
        "desired_service_placements": [],
        "desired_services": [],
        "desired_dependencies": [],
    }
}

EMPTY_ACTUAL_RESPONSE = {
    "data": {
        "devices": [],
        "virtual_machines": [],
        "interfaces": [],
        "ip_addresses": [],
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
def test_production_policy_emits_intent_effect_summary_not_old_code(tmp_path):
    respx.post(f"{BASE_URL}/api/graphql/").mock(
        side_effect=[
            httpx.Response(200, json=ONE_NODE_DESIRED_RESPONSE),
            httpx.Response(200, json=EMPTY_ACTUAL_RESPONSE),
        ]
    )
    cfg = make_config(tmp_path)

    envelope = build_drift(cfg)

    assert envelope.ok is True
    [target] = [t for t in envelope.data.targets if t.target.slug == "agok"]
    codes = [d.code for d in target.diffs]
    assert "intent_effect_summary" in codes, f"expected new code, got {codes}"
    assert "derived_value_provenance" not in codes, "old code must not be emitted alongside the new one"
