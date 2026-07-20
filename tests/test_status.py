import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from nctl_core.config import Config
from nctl_core.dumps import NodeDump, NodeIdentity
from nctl_core.nautobot import NautobotConnectionError, NautobotInfo
from nctl_core.status import DumpsStatus, StatusData, _check_dumps, _git_submodule_status, build_status
from nctl_core.output import Envelope

GIT_ENV = ["-c", "user.name=test", "-c", "user.email=test@example.com", "-c", "init.defaultBranch=main"]


def run_git(args, cwd):
    subprocess.run(["git", *GIT_ENV, *args], cwd=cwd, check=True, capture_output=True, text=True)


def make_repo_with_submodule(tmp_path, dirty: bool = False, deinit: bool = False) -> Path:
    inner = tmp_path / "inner"
    inner.mkdir()
    run_git(["init"], inner)
    (inner / "README").write_text("hello")
    run_git(["add", "README"], inner)
    run_git(["commit", "-m", "init"], inner)

    outer = tmp_path / "outer"
    outer.mkdir()
    run_git(["init"], outer)
    (outer / "placeholder").write_text("x")
    run_git(["add", "placeholder"], outer)
    run_git(["commit", "-m", "init"], outer)
    subprocess.run(
        ["git", *GIT_ENV, "-c", "protocol.file.allow=always", "submodule", "add", str(inner), "sub"],
        cwd=outer, check=True, capture_output=True, text=True,
    )
    run_git(["commit", "-m", "add submodule"], outer)

    if dirty:
        (outer / "sub" / "README").write_text("changed")
    if deinit:
        subprocess.run(["git", "submodule", "deinit", "-f", "sub"], cwd=outer, check=True, capture_output=True, text=True)

    return outer


def test_git_submodule_status_clean(tmp_path):
    outer = make_repo_with_submodule(tmp_path)
    submodules = _git_submodule_status(outer)
    assert len(submodules) == 1
    assert submodules[0].name == "sub"
    assert submodules[0].state == "clean"


def test_git_submodule_status_modified(tmp_path):
    outer = make_repo_with_submodule(tmp_path, dirty=True)
    submodules = _git_submodule_status(outer)
    assert submodules[0].state == "modified"


def test_git_submodule_status_uninitialized(tmp_path):
    outer = make_repo_with_submodule(tmp_path, deinit=True)
    submodules = _git_submodule_status(outer)
    assert submodules[0].state == "uninitialized"


def make_dump_dir(tmp_path) -> Path:
    dumps_dir = tmp_path / "dumps"
    dumps_dir.mkdir()
    dump = {
        "schema_version": "nodeutils.inventory.v1",
        "collector": "nodeutils",
        "identity": {"hostname": "agpc"},
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "facts": {},
        "self_reported": {},
    }
    (dumps_dir / "agpc.json").write_text(json.dumps(dump))
    return dumps_dir


def make_config(tmp_path, dumps_dir: Path, repo_root: Path) -> Config:
    config_path = tmp_path / "nctl.toml"
    body = f"""
[nautobot]
url = "http://nautobot.test"

[inventory]
dumps_dir = "{dumps_dir}"

[events]
log_dir = "{tmp_path / 'events'}"

[ansible]
playbook_dir = "{tmp_path / 'ansible_agdev'}"
inventory = "inventories/generated/hosts_intent.yml"

[repo]
root = "{repo_root}"
"""
    config_path.write_text(body)
    return Config.load(config_path)


def test_check_dumps_computes_age_hours(tmp_path):
    dumps_dir = make_dump_dir(tmp_path)
    cfg = make_config(tmp_path, dumps_dir, tmp_path)
    status = _check_dumps(cfg)
    assert isinstance(status, DumpsStatus)
    assert status.hosts[0].hostname == "agpc"
    assert status.hosts[0].age_hours < 0.1
    assert status.errors == []


def test_build_status_degrades_independently_on_nautobot_failure(tmp_path, monkeypatch):
    dumps_dir = make_dump_dir(tmp_path)
    outer = make_repo_with_submodule(tmp_path)
    cfg = make_config(tmp_path, dumps_dir, outer)

    class FailingClient:
        def __init__(self, *a, **kw):
            pass

        def ping(self):
            raise NautobotConnectionError("connection refused")

        def close(self):
            pass

    monkeypatch.setattr("nctl_core.status.NautobotClient", FailingClient)

    envelope = build_status(cfg)
    assert envelope.ok is False
    assert envelope.data.nautobot.reachable is False
    # dumps and submodules still populated despite the nautobot failure
    assert envelope.data.dumps.hosts[0].hostname == "agpc"
    assert envelope.data.submodules[0].state == "clean"
    assert any(e.code == "nautobot_unreachable" for e in envelope.errors)


def test_build_status_not_ok_when_nautobot_unauthenticated(tmp_path, monkeypatch):
    dumps_dir = make_dump_dir(tmp_path)
    outer = make_repo_with_submodule(tmp_path)
    cfg = make_config(tmp_path, dumps_dir, outer)

    class UnauthClient:
        def __init__(self, *a, **kw):
            pass

        def ping(self):
            return NautobotInfo(reachable=True, url="http://nautobot.test", authenticated=False)

        def close(self):
            pass

    monkeypatch.setattr("nctl_core.status.NautobotClient", UnauthClient)

    envelope = build_status(cfg)
    assert envelope.ok is False
    assert envelope.data.nautobot.reachable is True
    assert envelope.data.nautobot.authenticated is False
    assert any(e.code == "nautobot_unauthenticated" for e in envelope.errors)


def test_build_status_not_ok_when_intent_graphql_missing(tmp_path, monkeypatch):
    dumps_dir = make_dump_dir(tmp_path)
    outer = make_repo_with_submodule(tmp_path)
    cfg = make_config(tmp_path, dumps_dir, outer)

    class GraphqlMissingClient:
        def __init__(self, *a, **kw):
            pass

        def ping(self):
            return NautobotInfo(
                reachable=True,
                url="http://nautobot.test",
                version="3.1.3",
                authenticated=True,
                intent_catalog=True,
                intent_graphql=False,
            )

        def close(self):
            pass

    monkeypatch.setattr("nctl_core.status.NautobotClient", GraphqlMissingClient)

    envelope = build_status(cfg)
    assert envelope.ok is False
    assert envelope.data.nautobot.intent_catalog is True
    assert envelope.data.nautobot.intent_graphql is False
    assert any(e.code == "intent_graphql_missing" for e in envelope.errors)


def test_build_status_ok_when_all_checks_pass(tmp_path, monkeypatch):
    dumps_dir = make_dump_dir(tmp_path)
    outer = make_repo_with_submodule(tmp_path)
    cfg = make_config(tmp_path, dumps_dir, outer)

    class OkClient:
        def __init__(self, *a, **kw):
            pass

        def ping(self):
            return NautobotInfo(
                reachable=True,
                url="http://nautobot.test",
                version="3.1.3",
                authenticated=True,
                intent_catalog=True,
                intent_graphql=True,
            )

        def close(self):
            pass

    monkeypatch.setattr("nctl_core.status.NautobotClient", OkClient)

    envelope = build_status(cfg)
    assert envelope.ok is True
    assert envelope.errors == []

    # golden schema shape: top-level and data keys are stable.
    parsed = json.loads(envelope.to_json())
    assert set(parsed.keys()) == {"schema", "generated_at", "ok", "data", "errors"}
    assert parsed["schema"] == "nctl.status.v1"
    assert set(parsed["data"].keys()) == {"operation_id", "nautobot", "dumps", "submodules"}
    assert set(parsed["data"]["nautobot"].keys()) == {
        "reachable",
        "url",
        "version",
        "authenticated",
        "intent_catalog",
        "intent_graphql",
    }
    assert set(parsed["data"]["dumps"].keys()) == {"dir", "hosts", "errors"}

    # the event log file was actually written, cross-referenced by operation_id.
    log_path = cfg.events.resolved_log_dir() / f"{parsed['data']['operation_id']}.jsonl"
    assert log_path.is_file()
    events = [json.loads(line)["event"] for line in log_path.read_text().splitlines()]
    assert events[0] == "started"
    assert events[-1] == "finished"


def test_render_status_text_points_to_drift_for_target_state(tmp_path, monkeypatch):
    from nctl_core.status import render_status_text

    dumps_dir = make_dump_dir(tmp_path)
    outer = make_repo_with_submodule(tmp_path)
    cfg = make_config(tmp_path, dumps_dir, outer)

    class OkClient:
        def __init__(self, *a, **kw):
            pass

        def ping(self):
            return NautobotInfo(
                reachable=True, url="http://nautobot.test", version="3.1.3",
                authenticated=True, intent_catalog=True, intent_graphql=True,
            )

        def close(self):
            pass

    monkeypatch.setattr("nctl_core.status.NautobotClient", OkClient)

    envelope = build_status(cfg)
    text = render_status_text(envelope)

    assert "target state: use `nctl drift --host SLUG`" in text
