from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import nctl_core.agent as agent
from nctl_core.agent_api import AgentApiError
from nctl_core.config import Config
from nctl_core.sources.desired import DesiredEndpoint, DesiredNode, DesiredNodeOperationalOverride, DesiredSnapshot

NODE_ID = "27818c12-fe15-4c9f-83d0-7949523f6c33"
KEY = "QUFBQUMzTnphQzFsWkRJMU5URTVBQUFBSUZmYWtlZWQyNTUxOWtleWJ5dGVzMDAwMDAwMDAwMDAwMDAwMA=="


def _config(tmp_path: Path) -> Config:
    path = tmp_path / "nctl.toml"
    known_hosts = tmp_path / "ssh" / "known_hosts"
    known_hosts.parent.mkdir()
    known_hosts.write_text(f"nctl-node-{NODE_ID} ssh-ed25519 {KEY}\n")
    path.write_text(
        f'''[nautobot]\nurl = "http://nautobot.test"\n\n[inventory]\ndumps_dir = "{tmp_path / 'dumps'}"\n\n[events]\nlog_dir = "{tmp_path / 'events'}"\n\n[ansible]\nplaybook_dir = "{tmp_path / 'ansible'}"\ninventory = "hosts.yml"\n\n[ssh]\nknown_hosts_file = "{known_hosts}"\n\n[agent]\nconnect_timeout_seconds = 1\n'''
    )
    return Config.load(path)


def _snapshot(os_name: str = "linux") -> DesiredSnapshot:
    return DesiredSnapshot(
        nodes=[DesiredNode(id=NODE_ID, slug="agpc", name="agpc", lifecycle="active", node_type="device")],
        endpoints=[DesiredEndpoint(id="endpoint", name="primary", endpoint_type="primary", node_id=NODE_ID, node_slug="agpc", mdns_name="agpc.local")],
        operational_overrides=[DesiredNodeOperationalOverride(id="override", node_id=NODE_ID, declared_host_os=os_name, ansible_port=2222)],
    )


def test_target_resolution_requires_exact_slug_and_enrollment(tmp_path):
    cfg = _config(tmp_path)
    target = agent._target_from_snapshot(cfg, _snapshot(), "agpc")
    assert target.endpoint == "agpc.local"
    assert target.ssh_port == 2222
    assert target.workdir == Path("/home/eiji/agent-work")
    with pytest.raises(agent.AgentError, match="no DesiredNode") as exc:
        agent._target_from_snapshot(cfg, _snapshot(), "ag")
    assert exc.value.code == "unknown_host"


def test_ssh_tunnel_args_close_the_managed_trust_policy(tmp_path):
    cfg = _config(tmp_path)
    argv = agent.ssh_tunnel_argv(cfg, agent._target_from_snapshot(cfg, _snapshot("macos"), "agpc"), 43123)
    assert argv[:6] == ["ssh", "-N", "-p", "2222", "-L", "127.0.0.1:43123:127.0.0.1:4096"]
    assert "IdentitiesOnly=yes" in argv
    assert str(cfg.resolved_agent_identity_file()) in argv
    assert "StrictHostKeyChecking=yes" in argv
    assert f"UserKnownHostsFile={cfg.resolved_ssh_known_hosts_file()}" in argv
    assert argv[-1] == "eiji@agpc.local"


class _Process:
    def __init__(self) -> None:
        self.terminated = False
        self.killed = False

    def poll(self): return None
    def terminate(self): self.terminated = True
    def wait(self, timeout): return 0
    def kill(self): self.killed = True


def test_open_tunnel_terminates_child_on_exit(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    process = _Process()
    monkeypatch.setattr(agent, "_ephemeral_port", lambda: 43123)
    with agent.open_tunnel(cfg, agent._target_from_snapshot(cfg, _snapshot(), "agpc"), popen=lambda *args, **kwargs: process) as port:
        assert port == 43123
    assert process.terminated is True


def test_status_reports_health_in_envelope(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    monkeypatch.setattr(agent, "resolve_agent_target", lambda cfg, host: agent._target_from_snapshot(cfg, _snapshot(), host))
    monkeypatch.setattr(agent, "open_tunnel", lambda cfg, target: _tunnel(43123))
    monkeypatch.setattr(agent, "_probe_health", lambda port, timeout: 200)
    envelope = agent.build_agent_status(cfg, "agpc")
    assert envelope.ok is True
    assert envelope.data.reachable is True
    assert envelope.data.health_status == 200
    assert envelope.data.health_url == "http://127.0.0.1:43123/doc"


def _tunnel(port):
    from contextlib import contextmanager
    @contextmanager
    def tunnel():
        yield port
    return tunnel()


def test_attach_passes_native_command_and_propagates_exit(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    monkeypatch.setattr(agent, "resolve_agent_target", lambda cfg, host: agent._target_from_snapshot(cfg, _snapshot(), host))
    monkeypatch.setattr(agent, "open_tunnel", lambda cfg, target: _tunnel(43123))
    monkeypatch.setattr(agent, "_probe_health", lambda port, timeout: 200)
    captured = {}
    monkeypatch.setattr(agent.subprocess, "run", lambda argv, check=False: captured.setdefault("result", subprocess.CompletedProcess(argv, 7)))
    assert agent.attach_agent(cfg, "agpc", "ses_existing") == 7
    assert captured["result"].args == ["opencode", "attach", "http://127.0.0.1:43123", "--dir", "/home/eiji/agent-work", "--session", "ses_existing"]


def test_run_timeout_keeps_created_session_and_uses_common_target_path(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    target = agent._target_from_snapshot(cfg, _snapshot(), "agpc")
    monkeypatch.setattr(agent, "resolve_agent_target", lambda cfg, host: target)
    monkeypatch.setattr(agent, "open_tunnel", lambda cfg, target: _tunnel(43123))
    monkeypatch.setattr(agent, "_probe_health", lambda port, timeout: 200)

    class FakeApi:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def create_session(self): return {"id": "ses_created"}
        def send_message(self, *args): raise AgentApiError("agent_timeout", "timed out", {"session_id": "ses_created"})

    monkeypatch.setattr(agent, "OpenCodeClient", FakeApi)
    envelope = agent.build_agent_run(cfg, "agpc", "inspect")
    assert envelope.ok is False
    assert envelope.data.session_id == "ses_created"
    assert envelope.data.outcome == "timed_out"
    assert envelope.errors[0].code == "agent_timeout"
