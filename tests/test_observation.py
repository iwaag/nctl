from __future__ import annotations

import base64
import json
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from nctl_core.artifacts import OperationArtifacts
from nctl_core.config import Config
from nctl_core.events import OperationLog
from nctl_core.jobs import NautobotJobResult
from nctl_core.observation import render_probe_hints, run_observation
from nctl_core.ssh_trust import derive_host_key_alias
from nctl_core.sources.desired import (
    DesiredEndpoint,
    DesiredNode,
    DesiredService,
    DesiredServicePlacement,
    DesiredSnapshot,
    DesiredWorkspace,
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


# Every host slug any test in this file passes to run_observation, pre-enrolled
# by _config() below so these tests exercise post-enrollment behavior, not the
# fix_sshkey Step 5 defense-in-depth guard (covered separately).
_ALL_TEST_HOSTS = ("node-a", "node-b", "node", "node.local")


def _config(tmp_path: Path) -> Config:
    playbook_dir = tmp_path / "ansible"
    playbook_dir.mkdir()
    known_hosts_file = tmp_path / "ssh" / "known_hosts"
    cfg = Config.model_validate(
        {
            "nautobot": {"url": "http://nautobot.invalid"},
            "inventory": {"dumps_dir": tmp_path / "dumps"},
            "ansible": {"playbook_dir": playbook_dir, "inventory": "unused.yml"},
            "reconcile": {
                "max_report_age_hours": 72,
                "max_report_bytes": 4096,
                "nodeutils_version": "a" * 40,
            },
            "ssh": {"known_hosts_file": known_hosts_file},
            "source_path": tmp_path / "nctl.toml",
        }
    )
    known_hosts_file.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"{derive_host_key_alias(_node_id(host))} ssh-ed25519 dGVzdC1vYnNlcnZhdGlvbi1maXh0dXJlLWtleQ== nctl:test\n"
        for host in _ALL_TEST_HOSTS
    ]
    known_hosts_file.write_text("".join(lines))
    return cfg


def _report(host: str, collected_at: datetime) -> str:
    return json.dumps(
        {
            "schema_version": "nodeutils.inventory.v2",
            "collector": {"name": "nodeutils"},
            "identity": {"hostname": host, "fqdn": f"{host}.local"},
            "collected_at": collected_at.isoformat(),
            "facts": {},
            "self_reported": {},
        }
    )


class FakeCommands:
    def __init__(self, reports: dict[str, str], *, collection_returncode: int = 0) -> None:
        self.reports = reports
        self.collection_returncode = collection_returncode
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
        return subprocess.CompletedProcess(
            args,
            self.collection_returncode if args[0] == "ansible-playbook" else 0,
            "",
            "",
        )


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
            lifecycle="active",
        ),
        DesiredService(
            id="svc-old", slug="old", name="old-service", display_name="Old",
            lifecycle="active",
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
        "service_probe_hints": {"dnsmasq": {}},
        "workspace_probe_hints": {},
    }


def test_probe_hints_include_an_active_service_endpoint() -> None:
    snapshot = _snapshot("node-a")
    endpoint = DesiredEndpoint(
        id="ollama-endpoint", name="ollama-api", endpoint_type="service",
        node_id=_node_id("node-a"), node_slug="node-a", dns_name="node-a.home.arpa",
        protocol="http", port=11434,
    )
    snapshot.endpoints.append(endpoint)
    snapshot.services = [DesiredService(id="svc-ollama", slug="ollama", name="ollama", lifecycle="active")]
    snapshot.placements = [
        DesiredServicePlacement(
            id="p1", service_id="svc-ollama", node_id=_node_id("node-a"), instance_name="ollama",
            deployment_profile="ollama", config_schema_version="1", endpoint_id="ollama-endpoint",
        )
    ]

    assert yaml.safe_load(render_probe_hints(snapshot, _node_id("node-a"))) == {
        "service_probe_hints": {"ollama": {"endpoint": "http://node-a.home.arpa:11434"}},
        "workspace_probe_hints": {},
    }


def test_probe_hints_manual_placement_gets_checks_and_no_endpoint() -> None:
    # manual_service: a manual placement is never reachability-probed; its
    # observe-only profile's file_exists check is the only observation hint.
    from nctl_core.reconcile.profiles import ProfileReconciliation

    snapshot = _snapshot("node-a")
    endpoint = DesiredEndpoint(
        id="swarmui-endpoint", name="swarmui-api", endpoint_type="service",
        node_id=_node_id("node-a"), node_slug="node-a", dns_name="node-a.home.arpa",
        protocol="http", port=7801,
    )
    snapshot.endpoints.append(endpoint)
    snapshot.services = [DesiredService(id="svc-swarmui", slug="swarmui", name="swarmui", lifecycle="active")]
    snapshot.placements = [
        DesiredServicePlacement(
            id="p1", service_id="svc-swarmui", node_id=_node_id("node-a"), instance_name="swarmui",
            deployment_profile="swarmui", config_schema_version="1", endpoint_id="swarmui-endpoint",
            management_mode="manual",
        )
    ]
    profile_reconciliation = {
        "swarmui": ProfileReconciliation.model_validate(
            {
                "observe_only": True,
                "checks": [{"kind": "file_exists", "path": "~/StabilityMatrix/Packages/SwarmUI"}],
            }
        ),
    }

    rendered = yaml.safe_load(render_probe_hints(snapshot, _node_id("node-a"), profile_reconciliation))

    assert rendered == {
        "service_probe_hints": {
            "swarmui": {"checks": [{"kind": "file_exists", "path": "~/StabilityMatrix/Packages/SwarmUI"}]}
        },
        "workspace_probe_hints": {},
    }


def test_probe_hints_attach_managed_files_from_profile_reconciliation() -> None:
    # fix_sshkey3 Step 4: an active placement whose deployment_profile has
    # ProfileAction.managed_files gets that metadata copied verbatim into
    # its service's probe hint -- the one metadata-owned path source.
    from nctl_core.reconcile.profiles import ManagedFileSpec, ProfileAction, ProfileReconciliation

    snapshot = _snapshot("node-a")
    snapshot.services = [
        DesiredService(
            id="svc-dns", slug="dns", name="dnsmasq", display_name="DNS",
            lifecycle="active",
        ),
    ]
    snapshot.placements = [
        DesiredServicePlacement(
            id="p1", service_id="svc-dns", node_id=_node_id("node-a"), instance_name="dns",
            deployment_profile="dnsmasq", config_schema_version="v1",
        ),
    ]
    profile_reconciliation = {
        "dnsmasq": ProfileReconciliation(
            action=ProfileAction(
                kind="dnsmasq_config",
                managed_files={"records": ManagedFileSpec(path="/etc/dnsmasq.d/nintent-records.conf")},
            )
        ),
    }

    rendered = yaml.safe_load(render_probe_hints(snapshot, _node_id("node-a"), profile_reconciliation))

    assert rendered == {
        "service_probe_hints": {
            "dnsmasq": {
                "managed_files": {"records": {"path": "/etc/dnsmasq.d/nintent-records.conf", "digest": "sha256"}},
            },
        },
        "workspace_probe_hints": {},
    }


def test_probe_hints_omit_managed_files_when_profile_has_none(tmp_path) -> None:
    from nctl_core.reconcile.profiles import ProfileAction, ProfileReconciliation

    snapshot = _snapshot("node-a")
    snapshot.services = [
        DesiredService(
            id="svc-grafana", slug="grafana", name="grafana", display_name="Grafana",
            lifecycle="active",
        ),
    ]
    snapshot.placements = [
        DesiredServicePlacement(
            id="p1", service_id="svc-grafana", node_id=_node_id("node-a"), instance_name="grafana",
            deployment_profile="grafana", config_schema_version="v1",
        ),
    ]
    profile_reconciliation = {
        "grafana": ProfileReconciliation(
            action=ProfileAction(kind="playbook", playbook="playbooks/monitoring/setup_grafana.yml")
        ),
    }

    rendered = yaml.safe_load(render_probe_hints(snapshot, _node_id("node-a"), profile_reconciliation))

    assert rendered == {"service_probe_hints": {"grafana": {}}, "workspace_probe_hints": {}}


def test_probe_hints_attach_bindings_from_profile_reconciliation() -> None:
    # service_relation Phase 3: an active placement whose deployment_profile
    # has ProfileAction.bindings gets that metadata copied verbatim into its
    # service's probe hint, alongside managed_files.
    from nctl_core.reconcile.profiles import BindingSlotSpec, ProfileAction, ProfileReconciliation

    snapshot = _snapshot("node-a")
    snapshot.services = [
        DesiredService(
            id="svc-node-agent", slug="node-agent", name="node-agent", display_name="Node Agent",
            lifecycle="active",
        ),
    ]
    snapshot.placements = [
        DesiredServicePlacement(
            id="p1", service_id="svc-node-agent", node_id=_node_id("node-a"), instance_name="node-agent",
            deployment_profile="node_agent", config_schema_version="v1",
        ),
    ]
    profile_reconciliation = {
        "node_agent": ProfileReconciliation(
            action=ProfileAction(
                kind="playbook",
                playbook="playbooks/agent/setup_opencode.yml",
                bindings={
                    "llm_provider": BindingSlotSpec(
                        config_file="~/.config/opencode/opencode.json",
                        json_path="provider.ollama.options.baseURL",
                    )
                },
            )
        ),
    }

    rendered = yaml.safe_load(render_probe_hints(snapshot, _node_id("node-a"), profile_reconciliation))

    assert rendered == {
        "service_probe_hints": {
            "node-agent": {
                "bindings": {
                    "llm_provider": {
                        "config_file": "~/.config/opencode/opencode.json",
                        "json_path": "provider.ollama.options.baseURL",
                    }
                },
            },
        },
        "workspace_probe_hints": {},
    }


def _workspace(node: str, *, lifecycle: str = "active", presence: str = "present", slug: str = "pj-example") -> DesiredWorkspace:
    return DesiredWorkspace(
        id=f"ws-{slug}", slug=slug, name=slug, lifecycle=lifecycle,
        source_remote_url="https://example.invalid/x.git", expected_path=f"/home/eiji/projects/{slug}",
        desired_presence=presence, node_id=_node_id(node), node_slug=node,
    )


def test_probe_hints_include_active_present_workspace_on_this_node() -> None:
    snapshot = _snapshot("node-a")
    snapshot.workspaces = [_workspace("node-a")]

    rendered = yaml.safe_load(render_probe_hints(snapshot, _node_id("node-a")))

    assert rendered == {
        "service_probe_hints": {},
        "workspace_probe_hints": {"pj-example": {"path": "/home/eiji/projects/pj-example"}},
    }


def test_probe_hints_omit_workspace_on_a_different_node() -> None:
    snapshot = _snapshot("node-a", "node-b")
    snapshot.workspaces = [_workspace("node-b")]

    rendered = yaml.safe_load(render_probe_hints(snapshot, _node_id("node-a")))

    assert rendered["workspace_probe_hints"] == {}


def test_probe_hints_omit_retired_or_absent_workspaces() -> None:
    snapshot = _snapshot("node-a")
    snapshot.workspaces = [
        _workspace("node-a", lifecycle="retired", slug="pj-retired"),
        _workspace("node-a", presence="absent", slug="pj-absent"),
    ]

    rendered = yaml.safe_load(render_probe_hints(snapshot, _node_id("node-a")))

    assert rendered["workspace_probe_hints"] == {}


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
    assert "dry_run" not in jobs.data
    assert jobs.data["max_report_bytes"] == 4096
    assert commands.calls[0][0] == "ansible-playbook"
    assert commands.calls[0][1:5] == [
        "-i",
        str(artifacts.root / "bootstrap/hosts_intent.yml"),
        "-i",
        str(tmp_path / "ansible/unused.yml"),
    ]
    assert commands.calls[0][-2:] == ["-e", f"nodeutils_version={'a' * 40}"]
    assert result.nodeutils_version == "a" * 40
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


def test_observation_never_accepts_old_report_after_collection_failure(tmp_path: Path) -> None:
    now = datetime(2026, 7, 16, 1, tzinfo=timezone.utc)
    artifacts, log = _operation(tmp_path)
    jobs = FakeJobRunner(artifacts)

    result = run_observation(
        _config(tmp_path), _snapshot("node-a"), ["node-a"], artifacts, log,
        command_runner=FakeCommands({"node-a": _report("node-a", now)}, collection_returncode=1),
        job_runner=jobs, now=now,
    )

    assert result.ok is False
    assert result.hosts[0].collected is False
    assert result.hosts[0].error == "node-a: nodeutils collection failed"
    assert jobs.data is None


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


def test_run_observation_rejects_unenrolled_host_before_any_ansible_call(tmp_path: Path) -> None:
    now = datetime(2026, 7, 16, 1, tzinfo=timezone.utc)
    artifacts, log = _operation(tmp_path)
    cfg = _config(tmp_path)
    # Not one of _ALL_TEST_HOSTS: _config() never enrolled it.
    snapshot = _snapshot("node-unenrolled")
    commands = FakeCommands({"node-unenrolled": _report("node-unenrolled", now)})

    with pytest.raises(ValueError, match="ssh_host_key_unenrolled"):
        run_observation(
            cfg, snapshot, ["node-unenrolled"], artifacts, log,
            command_runner=commands, job_runner=FakeJobRunner(artifacts), now=now,
        )

    assert commands.calls == []


def test_probe_hints_resolve_profile_checks_against_placement_config() -> None:
    # autotask_intent Step 1: a cron_task-style observe_only profile with a
    # `path_from_config` check renders a fully-resolved `checks` hint, so
    # nodeutils never needs to understand placement config.
    from nctl_core.reconcile.profiles import ProfileReconciliation

    snapshot = _snapshot("node-a")
    snapshot.services = [DesiredService(id="svc-hb", slug="heartbeat-cron", name="heartbeat-cron", lifecycle="active")]
    snapshot.placements = [
        DesiredServicePlacement(
            id="p1", service_id="svc-hb", node_id=_node_id("node-a"), instance_name="heartbeat",
            deployment_profile="cron_task", config_schema_version="1",
            config={"script_path": "/home/eiji/mycron/heartbeat.sh"},
        )
    ]
    profile_reconciliation = {
        "cron_task": ProfileReconciliation.model_validate(
            {"observe_only": True, "checks": [{"kind": "file_exists", "path_from_config": "script_path"}]}
        ),
    }

    rendered = yaml.safe_load(render_probe_hints(snapshot, _node_id("node-a"), profile_reconciliation))

    assert rendered == {
        "service_probe_hints": {
            "heartbeat-cron": {
                "checks": [{"kind": "file_exists", "path": "/home/eiji/mycron/heartbeat.sh"}],
            }
        },
        "workspace_probe_hints": {},
    }


def test_probe_hints_copy_only_manual_process_identity() -> None:
    snapshot = _snapshot("node-a")
    snapshot.services = [DesiredService(id="svc", slug="worker", name="worker", lifecycle="active")]
    snapshot.placements = [
        DesiredServicePlacement(
            id="p1", service_id="svc", node_id=_node_id("node-a"), instance_name="worker",
            deployment_profile="manual_toolchain", management_mode="manual", config_schema_version="1",
            config={"process_pattern": "worker.*serve", "unrelated": "not rendered"},
        )
    ]

    rendered = yaml.safe_load(render_probe_hints(snapshot, _node_id("node-a")))

    assert rendered == {
        "service_probe_hints": {"worker": {"process_pattern": "worker.*serve"}},
        "workspace_probe_hints": {},
    }


def test_probe_hints_missing_check_config_key_is_a_render_error() -> None:
    # A missing/empty config key at render time is a validation error for
    # that placement, not a silent skip (README_DEV lesson 1).
    from nctl_core.reconcile.profiles import CheckResolutionError, ProfileReconciliation

    snapshot = _snapshot("node-a")
    snapshot.services = [DesiredService(id="svc-hb", slug="heartbeat-cron", name="heartbeat-cron", lifecycle="active")]
    snapshot.placements = [
        DesiredServicePlacement(
            id="p1", service_id="svc-hb", node_id=_node_id("node-a"), instance_name="heartbeat",
            deployment_profile="cron_task", config_schema_version="1", config={},
        )
    ]
    profile_reconciliation = {
        "cron_task": ProfileReconciliation.model_validate(
            {"observe_only": True, "checks": [{"kind": "file_exists", "path_from_config": "script_path"}]}
        ),
    }

    with pytest.raises(CheckResolutionError, match="script_path"):
        render_probe_hints(snapshot, _node_id("node-a"), profile_reconciliation)
