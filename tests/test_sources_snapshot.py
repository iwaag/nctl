from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import respx

from nctl_core.config import Config
from nctl_core.nautobot import NautobotClient
from nctl_core.sources.snapshot import build_source_snapshot

BASE_URL = "http://nautobot.test"

DESIRED_DATA = {
    "desired_nodes": [],
    "desired_endpoints": [],
    "desired_ip_ranges": [],
    "desired_node_operational_configs": [],
    "desired_service_placements": [],
    "desired_services": [],
    "desired_dependencies": [],
}
ACTUAL_DATA = {
    "devices": [],
    "virtual_machines": [],
    "interfaces": [],
    "ip_addresses": [],
}


def make_config(tmp_path: Path, dumps_dir: Path) -> Config:
    config_path = tmp_path / "nctl.toml"
    config_path.write_text(
        f"""
[nautobot]
url = "{BASE_URL}"

[inventory]
dumps_dir = "{dumps_dir}"

[events]
log_dir = "{tmp_path / 'events'}"

[ansible]
playbook_dir = "{tmp_path / 'ansible_agdev'}"
inventory = "inventories/generated/hosts_intent.yml"

[repo]
root = "{tmp_path}"
"""
    )
    return Config.load(config_path)


def make_dump_dir(tmp_path: Path) -> Path:
    dumps_dir = tmp_path / "dumps"
    dumps_dir.mkdir()
    dump = {
        "schema_version": "nodeutils.inventory.v1",
        "identity": {"hostname": "agpc"},
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "facts": {"system": "linux"},
        "self_reported": {},
    }
    (dumps_dir / "agpc.json").write_text(json.dumps(dump))
    (dumps_dir / "broken.json").write_text("{not json")
    return dumps_dir


@respx.mock
def test_build_source_snapshot_fetches_each_source_once_and_degrades_dumps(tmp_path):
    dumps_dir = make_dump_dir(tmp_path)
    cfg = make_config(tmp_path, dumps_dir)

    route = respx.post(f"{BASE_URL}/api/graphql/").mock(
        side_effect=lambda request: httpx.Response(
            200,
            json={"data": DESIRED_DATA if b"desired_nodes" in request.content else ACTUAL_DATA},
        )
    )
    client = NautobotClient(BASE_URL, "tok")

    snapshot = build_source_snapshot(cfg, client)

    assert route.call_count == 2  # one desired fetch, one actual fetch
    assert snapshot.desired.nodes == []
    assert snapshot.actual.devices == []
    assert [o.hostname for o in snapshot.observed] == ["agpc"]
    assert len(snapshot.observed_errors) == 1
    assert "broken.json" in snapshot.observed_errors[0]
