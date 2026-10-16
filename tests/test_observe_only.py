"""`nctl observe` (project_migration p5): observation only, explicit host sets, per-host outcomes."""

from __future__ import annotations

import base64
import json
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from nctl_core import observe_only
from nctl_core.config import Config
from nctl_core.jobs import NautobotJobResult
from nctl_core.observe_only import UsageError, run_observe
from nctl_core.sources.desired import DesiredEndpoint, DesiredNode, DesiredServicePlacement, DesiredService, DesiredSnapshot
from nctl_core.ssh_enroll import SshProbeRunner
from nctl_core.ssh_trust import derive_host_key_alias

NOW = datetime(2026, 10, 16, 1, tzinfo=timezone.utc)
KEY = "dGVzdC1vYnNlcnZlLW9ubHkta2V5LWJ5dGVz"
HOSTS = ("node-a", "node-b", "asleep")


def _id(host: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"observe-only:{host}"))


def _snapshot(*, inactive: tuple[str, ...] = ()) -> DesiredSnapshot:
    nodes = [
        DesiredNode(id=_id(h), slug=h, name=h, lifecycle="active", node_type="device")
        for h in HOSTS
    ] + [DesiredNode(id=_id(h), slug=h, name=h, lifecycle="retired", node_type="device") for h in inactive]
    endpoints = [
        DesiredEndpoint(id=f"ep-{n.slug}", name="primary", endpoint_type="primary", node_id=n.id,
                        node_slug=n.slug, mdns_name=f"{n.slug}.local")
        for n in nodes
    ]
    # A placement whose service is not running is actionable drift for `reconcile`;
    # `observe` must not care.
    service = DesiredService(id="svc", slug="web", name="web", lifecycle="active")
    placement = DesiredServicePlacement(
        id="pl", service_id="svc", node_id=_id("node-a"), deployment_profile="static_web_app", instance_name="web", config_schema_version="1",
        desired_state="active", management_mode="nctl_managed",
    )
    return DesiredSnapshot(nodes=nodes, endpoints=endpoints, services=[service], placements=[placement])


def _config(tmp_path: Path) -> Config:
    (tmp_path / "ansible").mkdir()
    known_hosts = tmp_path / "ssh" / "known_hosts"
    known_hosts.parent.mkdir(parents=True)
    known_hosts.write_text("".join(f"{derive_host_key_alias(_id(h))} ssh-ed25519 {KEY} nctl:test\n" for h in HOSTS))
    return Config.model_validate({
        "nautobot": {"url": "http://nautobot.invalid"},
        "inventory": {"dumps_dir": tmp_path / "dumps"},
        "events": {"log_dir": tmp_path / "events"},
        "ansible": {"playbook_dir": tmp_path / "ansible", "inventory": "unused.yml"},
        "reconcile": {"nodeutils_version": "a" * 40, "lock_path": tmp_path / "reconcile.lock", "max_report_bytes": 8192},
        "ssh": {"known_hosts_file": known_hosts},
        "source_path": tmp_path / "nctl.toml",
    })


def _probe(unreachable: set[str]) -> SshProbeRunner:
    def keyscan(host, port, timeout):
        name = host.removesuffix(".local")
        if name in unreachable:
            return subprocess.CompletedProcess([], 1, "", "timeout")
        return subprocess.CompletedProcess([], 0, f"{host} ssh-ed25519 {KEY}\n", "")
    return SshProbeRunner(
        keyscan=keyscan,
        effective_config=lambda host, port: subprocess.CompletedProcess([], 0, "", ""),
        keygen_find=lambda path, host: subprocess.CompletedProcess([], 0, "", ""),
    )


def _report(host: str) -> str:
    return json.dumps({
        "schema_version": "nodeutils.inventory.v2", "collector": {"name": "nodeutils"},
        "identity": {"hostname": host, "fqdn": f"{host}.local"}, "collected_at": NOW.isoformat(),
        "facts": {}, "self_reported": {},
    })


class Commands:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args, cwd, timeout):
        self.calls.append(args)
        limit = args[args.index("--limit") + 1].split(",")
        if args[0] == "ansible":
            tree = Path(args[args.index("--tree") + 1])
            for host in limit:
                (tree / host).write_text(json.dumps({"encoding": "base64", "content": base64.b64encode(_report(host).encode()).decode()}))
        return subprocess.CompletedProcess(args, 0, "", "")


class Jobs:
    def __init__(self, outcomes: dict[str, str] | None = None) -> None:
        self.outcomes = outcomes or {}

    def run(self, job_name, data, **kwargs):
        sources = [row["source"] for row in json.loads(data["report_batch"])["reports"]]
        path = self.root / kwargs["artifact_relative_path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": "nodeutils.ingest.summary.v1", "results": [
            {"source": s, "outcome": self.outcomes.get(s, "updated")} for s in sources]}))
        return NautobotJobResult(job_name=job_name, job_id="j", job_result_id="jr", job_result_url="/jr",
                                 status="completed", poll_count=1, artifact_name=kwargs["artifact_name"], artifact_path=str(path))


def _observe(tmp_path, hosts, *, unreachable=(), outcomes=None, **kwargs):
    cfg = _config(tmp_path)
    commands, jobs = Commands(), Jobs(outcomes)
    jobs.root = tmp_path / "job-artifacts"
    envelope = run_observe(
        cfg, hosts, command_runner=commands, job_runner=jobs, snapshot_fetcher=kwargs.pop("snapshot", _snapshot),
        ssh_probe=_probe(set(unreachable)), now=lambda: NOW, **kwargs,
    )
    return envelope, commands


@pytest.fixture(autouse=True)
def _no_planning_or_actuation(monkeypatch):
    """Observation never plans, computes drift or dispatches a reconciler."""
    from nctl_core.reconcile import planner
    from nctl_core.reconcile.actions import dispatch
    from nctl_core import drift_render

    def forbidden(*_a, **_k):
        raise AssertionError("observe must not plan, compute drift or dispatch an action")

    monkeypatch.setattr(planner, "build_plan", forbidden)
    monkeypatch.setattr(dispatch, "execute_action", forbidden)
    monkeypatch.setattr(drift_render, "fetch_and_compute_drift", forbidden)


def test_explicit_hosts_are_observed_and_nothing_else_runs(tmp_path):
    envelope, commands = _observe(tmp_path, ["node-b", "node-a"], operation_id="01JBBBBBBBBBBBBBBBBBBBBBBB")
    assert envelope.ok and envelope.data.state == "observed"
    assert envelope.data.operation_id == "01JBBBBBBBBBBBBBBBBBBBBBBB"
    assert [(h.host, h.outcome) for h in envelope.data.hosts] == [("node-a", "observed"), ("node-b", "observed")]
    # Only the collection playbook and the report retrieval ran — no service playbook for node-a's placement.
    assert [call[0] for call in commands.calls] == ["ansible-playbook", "ansible"]
    assert commands.calls[0][4].endswith("unused.yml") and "run_nodeutils_collect.yml" in commands.calls[0][5]
    result = json.loads((tmp_path / "events" / envelope.data.operation_id / "result.json").read_text())
    assert result["schema"] == "nctl.observe.v1" and result["data"]["state"] == "observed"


def test_unknown_host_is_a_usage_error_before_anything_runs(tmp_path):
    with pytest.raises(UsageError, match="nonesuch"):
        _observe(tmp_path, ["node-a", "nonesuch"])
    assert not (tmp_path / "events").exists() or not any((tmp_path / "events").iterdir())


def test_inactive_host_is_refused_not_dropped(tmp_path):
    with pytest.raises(UsageError, match="not observable"):
        _observe(tmp_path, ["gone"], snapshot=lambda: _snapshot(inactive=("gone",)))


def test_unavailable_host_is_not_contacted_and_others_still_observed(tmp_path):
    envelope, commands = _observe(tmp_path, ["node-a", "asleep"], unreachable={"asleep"})
    assert not envelope.ok and envelope.data.state == "partial"
    by_host = {h.host: h for h in envelope.data.hosts}
    assert by_host["node-a"].outcome == "observed"
    assert by_host["asleep"].outcome == "unavailable" and by_host["asleep"].ssh_status == "unreachable"
    assert all(call[call.index("--limit") + 1] == "node-a" for call in commands.calls)
    assert [e.code for e in envelope.errors] == ["host_unavailable"]


def test_every_host_unavailable_is_failed_and_runs_no_ansible(tmp_path):
    envelope, commands = _observe(tmp_path, ["asleep"], unreachable={"asleep"})
    assert envelope.data.state == "failed" and commands.calls == []


def test_ingest_failure_is_its_own_outcome(tmp_path):
    envelope, _ = _observe(tmp_path, ["node-a", "node-b"], outcomes={"node-b": "skipped"})
    by_host = {h.host: h.outcome for h in envelope.data.hosts}
    assert by_host == {"node-a": "observed", "node-b": "ingest_failed"}
    assert envelope.data.state == "partial"


def test_default_scope_is_every_exported_node_and_lists_the_rest(tmp_path):
    envelope, _ = _observe(tmp_path, [], snapshot=lambda: _snapshot(inactive=("gone",)), unreachable={"asleep"})
    assert envelope.data.scope == "default"
    assert envelope.data.requested == ["asleep", "node-a", "node-b"]
    assert [e["desired_node_slug"] for e in envelope.data.skipped] == ["gone"]


def test_operation_id_must_be_a_fresh_ulid(tmp_path):
    with pytest.raises(UsageError, match="malformed"):
        _observe(tmp_path, ["node-a"], operation_id="not-a-ulid")
    (tmp_path / "events").mkdir(exist_ok=True)
    (tmp_path / "events" / "01JCCCCCCCCCCCCCCCCCCCCCCC.jsonl").write_text("")
    with pytest.raises(UsageError, match="already in use"):
        observe_only.check_operation_id("01JCCCCCCCCCCCCCCCCCCCCCCC", tmp_path / "events")


def test_cli_exit_codes(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from nctl_core.cli import main
    from nctl_core.output import Envelope

    cfg = _config(tmp_path)
    monkeypatch.setattr(main, "_load_config", lambda _path: cfg)
    monkeypatch.setattr(main, "run_observe", lambda *_a, **_k: (_ for _ in ()).throw(UsageError("no desired node 'x'")))
    assert CliRunner().invoke(main.app, ["observe", "x"]).exit_code == 2
    partial = Envelope(schema="nctl.observe.v1", generated_at=NOW, ok=False,
                       data=observe_only.ObserveData(operation_id="01JDDDDDDDDDDDDDDDDDDDDDDD", state="partial"))
    monkeypatch.setattr(main, "run_observe", lambda *_a, **_k: partial)
    result = CliRunner().invoke(main.app, ["observe", "--json"])
    assert result.exit_code == 1, (result.output, result.exception)
    assert json.loads(result.stdout)["data"]["state"] == "partial"
