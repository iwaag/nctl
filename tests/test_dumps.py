import json
from pathlib import Path

import pytest

from nctl_core.dumps import DumpError, load_dump, scan_dumps

VALID = {
    "schema_version": "nodeutils.inventory.v2",
    "collector": "nodeutils",
    "identity": {"hostname": "agpc"},
    "collected_at": "2026-07-14T12:00:00+00:00",
    "facts": {"os": "linux"},
    "self_reported": {},
}


def write_json(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data))
    return path


def test_load_dump_valid(tmp_path):
    path = write_json(tmp_path / "agpc.json", VALID)
    dump = load_dump(path)
    assert dump.identity.hostname == "agpc"
    assert dump.schema_version == "nodeutils.inventory.v2"
    assert dump.facts == {"os": "linux"}


def test_load_dump_yaml(tmp_path):
    path = tmp_path / "agpc.yaml"
    path.write_text(
        "schema_version: nodeutils.inventory.v2\n"
        "collector: nodeutils\n"
        "identity:\n  hostname: agpc\n"
        "collected_at: '2026-07-14T12:00:00+00:00'\n"
    )
    dump = load_dump(path)
    assert dump.identity.hostname == "agpc"


def test_load_dump_wrong_schema_version(tmp_path):
    bad = {**VALID, "schema_version": "nodeutils.inventory.v0"}
    path = write_json(tmp_path / "agpc.json", bad)
    with pytest.raises(DumpError, match="schema_version"):
        load_dump(path)


def test_load_dump_malformed_json(tmp_path):
    path = tmp_path / "agpc.json"
    path.write_text("{not json")
    with pytest.raises(DumpError):
        load_dump(path)


def test_load_dump_missing_required_field(tmp_path):
    bad = {k: v for k, v in VALID.items() if k != "identity"}
    path = write_json(tmp_path / "agpc.json", bad)
    with pytest.raises(DumpError):
        load_dump(path)


def test_scan_dumps_missing_dir(tmp_path):
    result = scan_dumps(tmp_path / "nope")
    assert result.dumps == []
    assert "not found" in result.errors[0]


def test_scan_dumps_mixed_valid_and_invalid(tmp_path):
    write_json(tmp_path / "agpc.json", VALID)
    write_json(tmp_path / "agstudio.json", {**VALID, "identity": {"hostname": "agstudio"}})
    (tmp_path / "broken.json").write_text("{not json")

    result = scan_dumps(tmp_path)
    hostnames = sorted(d.identity.hostname for d in result.dumps)
    assert hostnames == ["agpc", "agstudio"]
    assert len(result.errors) == 1
    assert "broken.json" in result.errors[0]


def test_scan_dumps_ignores_nctl_probe_config(tmp_path):
    write_json(tmp_path / "agstudio.json", {**VALID, "identity": {"hostname": "agstudio"}})
    (tmp_path / "nctl-probe-config.yaml").write_text("service_probe_hints: {}\n")

    result = scan_dumps(tmp_path)

    assert [dump.identity.hostname for dump in result.dumps] == ["agstudio"]
    assert result.errors == []
