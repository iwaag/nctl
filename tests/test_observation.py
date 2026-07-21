from __future__ import annotations

import base64
import json
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from nctl_core.artifacts import OperationArtifacts
from nctl_core.config import Config
from nctl_core.events import OperationLog
from nctl_core.jobs import NautobotJobResult
from nctl_core.observation import render_probe_hints, run_observation
from nctl_core.sources.desired import (
    DesiredEndpoint,
    DesiredNode,
    DesiredService,
    DesiredServicePlacement,
    DesiredSnapshot,
)


def _node_id(host: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"nctl-test-node:{host}"))


def _snapshot(*hosts: str) -> DesiredSnapshot:
    nodes = [
        DesiredNode(id=_node_id(host), slug=host, name=host, lifecycle="active", node_type="device")
        for host in hosts
    ]
    endpoints = [
        DesiredEndpoint(
            id=f"endpoint-{host}", name="primary", endpoint_type="primary",
            node_id=_node_id(host), node_slug=host, mdns_name=f"{host}.local",
        )
        for host in hosts
    ]
    return DesiredSnapshot(nodes=nodes, endpoints=endpoints)


def _config(tmp_path: Path) -> Config:
    playbook_dir = tmp_path / "ansible"
    playbook_dir.mkdir()
    return Config.model_validate(
        {
            "nautobot": {"url": "http://nautobot.invalid"},
            "inventory": {"dumps_dir": tmp_path / "dumps"},
            "ansible": {"playbook_dir": playbook_dir, "inventory": "unused.yml"},
            "reconcile": {"max_report_age_hours": 72, "max_report_bytes": 4096},
            "source_path": tmp_path / "nctl.toml",
        }
    )


def _report(host: str, collected_at: datetime) -> str:
    return json.dumps(
        {
            "schema_version": "nodeutils.inventory.v1",
            "collector": {"name": "nodeutils"},
            "identity": {"hostname": host, "fqdn": f"{host}.local"},
            "collected_at": collected_at.isoformat(),
            "facts": {},
            "self_reported": {},
        }
    )


class FakeCommands:
    def __init__(self, reports: dict[str, str]) -> None:
        self.reports = reports
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], cwd: Path, timeout: float | None):
        self.calls.append(args)
        if args[0] == "ansible":
            tree = Path(args[args.index("--tree") + 1])
            for host, report in self.reports.items():
                (tree / host).write_text(
                    json.dumps(
                        {
                            "encoding": "base64",
                            "content": base64.b64encode(report.encode()).decode(),
                        }
                    )
                )
        return subprocess.CompletedProcess(args, 0, "", "")


class FakeJobRunner:
    def __init__(self, artifacts: OperationArtifacts, outcomes: dict[str, str] | None = None) -> None:
        self.artifacts = artifacts
        self.outcomes = outcomes or {}
        self.data = None

    def run(self, job_name, data, **kwargs):
        self.data = data
        sources = [row["source"] for row in json.loads(data["report_batch"])["reports"]]
        path = self.artifacts.write_json(
            kwargs["artifact_relative_path"],
            {
                "schema_version": "nodeutils.ingest.summary.v1",
                "dry_run": False,
                "summary": {"total": len(sources)},
                "results": [
                    {"source": source, "outcome": self.outcomes.get(source, "updated")}
                    for source in sources
                ],
            },
        )
        return NautobotJobResult(
            job_name=job_name, job_id="job", job_result_id="result",
            job_result_url="/result", status="completed", poll_count=1,
            artifact_name=kwargs["artifact_name"], artifact_path=str(path),
        )


def _operation(tmp_path: Path) -> tuple[OperationArtifacts, OperationLog]:
    artifacts = OperationArtifacts.create(tmp_path / "events", "01JOBSERVE")
    return artifacts, OperationLog("observe", tmp_path / "logs", "01JOBSERVE")


def test_probe_hints_are_active_authoritative_service_names() -> None:
    snapshot = _snapshot("node-a")
    snapshot.services = [
        DesiredService(
            id="svc-dns", slug="dns", name="dnsmasq", display_name="DNS",
            service_type="system", lifecycle="active", catalog_namespace="x", catalog_metadata_name="dns",
        ),
        DesiredService(
            id="svc-old", slug="old", name="old-service", display_name="Old",
            service_type="system", lifecycle="active", catalog_namespace="x", catalog_metadata_name="old",
        ),
    ]
    snapshot.placements = [
        DesiredServicePlacement(
            id="p1", service_id="svc-dns", node_id=_node_id("node-a"), instance_name="dns",
            deployment_profile="systemd", config_schema_version="v1",
        ),
        DesiredServicePlacement(
            id="p2", service_id="svc-old", node_id=_node_id("node-a"), instance_name="old",
            desired_state="absent", deployment_profile="systemd", config_schema_version="v1",
        ),
    ]

    assert yaml.safe_load(render_probe_hints(snapshot, _node_id("node-a"))) == {
        "service_probe_hints": {"dnsmasq": {}}
    }


def test_observation_collects_caches_and_ingests_all_hosts(tmp_path: Path) -> None:
    now = datetime(2026, 7, 16, 1, tzinfo=timezone.utc)
    artifacts, log = _operation(tmp_path)
    commands = FakeCommands({host: _report(host, now) for host in ("node-a", "node-b")})
    jobs = FakeJobRunner(artifacts)

    result = run_observation(
        _config(tmp_path), _snapshot("node-a", "node-b"), ["node-b", "node-a"],
        artifacts, log, command_runner=commands, job_runner=jobs, now=now,
    )

    assert result.ok is True
    assert [row.host for row in result.hosts] == ["node-a", "node-b"]
    assert all(row.ingest_outcome == "updated" for row in result.hosts)
    assert (tmp_path / "dumps/node-a.json").is_file()
    assert (artifacts.root / "reports/node-b.json").is_file()
    assert jobs.data["dry_run"] is False
    assert jobs.data["max_report_bytes"] == 4096
    assert commands.calls[0][0] == "ansible-playbook"
    assert commands.calls[0][1:5] == [
        "-i",
        str(artifacts.root / "bootstrap/hosts_intent.yml"),
        "-i",
        str(tmp_path / "ansible/unused.yml"),
    ]
    assert commands.calls[1][0] == "ansible"
    assert commands.calls[1][1:5] == [
        "-i",
        str(artifacts.root / "bootstrap/hosts_intent.yml"),
        "-i",
        str(tmp_path / "ansible/unused.yml"),
    ]


def test_observation_ingests_available_hosts_but_reports_partial_failure(tmp_path: Path) -> None:
    now = datetime(2026, 7, 16, 1, tzinfo=timezone.utc)
    artifacts, log = _operation(tmp_path)
    jobs = FakeJobRunner(artifacts)

    result = run_observation(
        _config(tmp_path), _snapshot("node-a", "node-b"), ["node-a", "node-b"],
        artifacts, log, command_runner=FakeCommands({"node-a": _report("node-a", now)}),
        job_runner=jobs, now=now,
    )

    by_host = {row.host: row for row in result.hosts}
    assert result.ok is False
    assert by_host["node-a"].ingest_outcome == "updated"
    assert by_host["node-b"].collected is False
    assert "node-b" in by_host["node-b"].error
    assert [row["source"] for row in json.loads(jobs.data["report_batch"])["reports"]] == ["node-a"]


def test_completed_job_with_skipped_report_is_failure(tmp_path: Path) -> None:
    now = datetime(2026, 7, 16, 1, tzinfo=timezone.utc)
    artifacts, log = _operation(tmp_path)

    result = run_observation(
        _config(tmp_path), _snapshot("node-a"), ["node-a"], artifacts, log,
        command_runner=FakeCommands({"node-a": _report("node-a", now)}),
        job_runner=FakeJobRunner(artifacts, {"node-a": "skipped"}), now=now,
    )

    assert result.ok is False
    assert result.hosts[0].ingest_outcome == "skipped"
    assert result.hosts[0].error == "ingest skipped report"


def test_observation_rejects_stale_and_wrong_identity_before_cache(tmp_path: Path) -> None:
    now = datetime(2026, 7, 16, 1, tzinfo=timezone.utc)
    artifacts, log = _operation(tmp_path)
    commands = FakeCommands(
        {
            "node-a": _report("other", now),
            "node-b": _report("node-b", now - timedelta(days=4)),
        }
    )

    result = run_observation(
        _config(tmp_path), _snapshot("node-a", "node-b"), ["node-a", "node-b"],
        artifacts, log, command_runner=commands, job_runner=FakeJobRunner(artifacts), now=now,
    )

    assert result.ok is False
    assert "identity does not match" in result.hosts[0].error
    assert "stale" in result.hosts[1].error
    assert not (tmp_path / "dumps").exists()


def test_observation_rejects_duplicate_canonical_identity(tmp_path: Path) -> None:
    now = datetime(2026, 7, 16, 1, tzinfo=timezone.utc)
    artifacts, log = _operation(tmp_path)
    shared = _report("node", now)

    result = run_observation(
        _config(tmp_path), _snapshot("node", "node.local"), ["node", "node.local"],
        artifacts, log, command_runner=FakeCommands({"node": shared, "node.local": shared}),
        job_runner=FakeJobRunner(artifacts), now=now,
    )

    assert result.ok is False
    assert all("duplicate" in row.error for row in result.hosts)
    assert not (tmp_path / "dumps").exists()
