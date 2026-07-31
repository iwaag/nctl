"""SSH-tunnelled, interactive access to node-local OpenCode agents."""

from __future__ import annotations

import socket
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

import httpx
from pydantic import BaseModel

from nctl_core.config import Config, ConfigError
from nctl_core.events import OperationLog
from nctl_core.hosts_intent import select_mdns_endpoint
from nctl_core.nautobot import NautobotClient, NautobotError
from nctl_core.output import Envelope, EnvelopeError
from nctl_core.sources.desired import DesiredNode, DesiredSnapshot, fetch_desired_snapshot
from nctl_core.ssh_enroll import SshStoreReadError, _resolve_node, load_managed_ssh_store
from nctl_core.ssh_trust import derive_host_key_alias

AGENT_STATUS_SCHEMA = "nctl.agent.status.v1"


class AgentError(Exception):
    def __init__(self, code: str, message: str, detail: dict[str, object] | None = None) -> None:
        self.code, self.detail = code, detail or {}
        super().__init__(message)


class AgentStatusData(BaseModel):
    operation_id: str = ""
    node_slug: str = ""
    endpoint: str = ""
    ssh_port: int = 22
    agent_port: int = 4096
    workdir: str = ""
    reachable: bool = False
    health_url: str = ""
    health_status: int | None = None


@dataclass(frozen=True)
class AgentTarget:
    node: DesiredNode
    endpoint: str
    ssh_port: int
    alias: str
    workdir: Path


def _target_from_snapshot(cfg: Config, snapshot: DesiredSnapshot, host: str) -> AgentTarget:
    node, error = _resolve_node(snapshot, host)
    if error is not None or node is None:
        raise AgentError("unknown_host", f"no DesiredNode with slug {host!r}", {"host": host})
    endpoint = select_mdns_endpoint([item for item in snapshot.endpoints if item.node_id == node.id])
    if endpoint is None or not endpoint.mdns_name:
        raise AgentError("node_without_mdns", f"DesiredNode {host!r} has no mDNS endpoint", {"host": host})
    override = next((item for item in snapshot.operational_overrides if item.node_id == node.id), None)
    ssh_port = override.ansible_port if override and override.ansible_port else 22
    declared_os = override.declared_host_os if override else None
    configured_workdir = cfg.agent.workdir_by_slug.get(host)
    if configured_workdir is not None:
        workdir = configured_workdir
    elif declared_os in {"macos", "darwin"}:
        workdir = cfg.agent.macos_workdir
    elif declared_os == "linux":
        workdir = cfg.agent.linux_workdir
    else:
        raise AgentError("agent_workdir_unresolved", f"DesiredNode {host!r} needs [agent].workdir_by_slug or a declared_host_os")
    alias = derive_host_key_alias(node.id)
    try:
        store = load_managed_ssh_store(cfg.resolved_ssh_known_hosts_file())
    except SshStoreReadError as exc:
        raise AgentError("ssh_store_read_failed", str(exc)) from exc
    if not store.entries_for(alias):
        raise AgentError(
            "ssh_unenrolled",
            f"{host!r} is not enrolled in the managed SSH trust store; run `nctl ssh enroll {host} ...` first",
            {"host": host, "alias": alias},
        )
    return AgentTarget(node=node, endpoint=endpoint.mdns_name, ssh_port=ssh_port, alias=alias, workdir=workdir)


def resolve_agent_target(cfg: Config, host: str) -> AgentTarget:
    try:
        token = cfg.nautobot.resolve_token()
    except ConfigError as exc:
        raise AgentError("nautobot_token_error", str(exc)) from exc
    client = NautobotClient(cfg.nautobot.url, token)
    try:
        return _target_from_snapshot(cfg, fetch_desired_snapshot(client), host)
    except NautobotError as exc:
        raise AgentError("desired_snapshot_failed", str(exc)) from exc
    finally:
        client.close()


def _ephemeral_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def ssh_tunnel_argv(cfg: Config, target: AgentTarget, local_port: int) -> list[str]:
    """Construct a closed SSH invocation using the same trust policy as Ansible."""
    known_hosts = str(cfg.resolved_ssh_known_hosts_file())
    identity_file = str(cfg.resolved_agent_identity_file())
    return [
        "ssh", "-N", "-p", str(target.ssh_port), "-L", f"127.0.0.1:{local_port}:127.0.0.1:{cfg.agent.port}",
        "-o", "ExitOnForwardFailure=yes", "-i", identity_file, "-o", "IdentitiesOnly=yes", "-o", f"HostKeyAlias={target.alias}",
        "-o", f"UserKnownHostsFile={known_hosts}", "-o", "StrictHostKeyChecking=yes",
        "-o", "CheckHostIP=no", "-o", "UpdateHostKeys=no", f"{cfg.agent.ssh_user}@{target.endpoint}",
    ]


@contextmanager
def open_tunnel(
    cfg: Config, target: AgentTarget, *, popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen
) -> Iterator[int]:
    local_port = _ephemeral_port()
    try:
        process = popen(ssh_tunnel_argv(cfg, target, local_port), stdin=subprocess.DEVNULL)
    except OSError as exc:
        raise AgentError("ssh_tunnel_start_failed", f"could not start SSH tunnel: {exc}") from exc
    try:
        yield local_port
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def _probe_health(local_port: int, timeout_seconds: float) -> int:
    url = f"http://127.0.0.1:{local_port}/doc"
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=min(2.0, timeout_seconds))
            return response.status_code
        except httpx.HTTPError as exc:
            last_error = exc
            time.sleep(0.2)
    raise AgentError("agent_unreachable", f"agent health endpoint did not respond within {timeout_seconds:g}s: {last_error}")


def build_agent_status(cfg: Config, host: str) -> Envelope[AgentStatusData]:
    op = OperationLog.start("agent status", cfg.events.resolved_log_dir())
    data = AgentStatusData(operation_id=op.operation_id, node_slug=host, agent_port=cfg.agent.port)
    try:
        target = resolve_agent_target(cfg, host)
        data = data.model_copy(update={"endpoint": target.endpoint, "ssh_port": target.ssh_port, "workdir": str(target.workdir)})
        op.emit("step_started", "opening SSH tunnel", host=host, endpoint=target.endpoint)
        with open_tunnel(cfg, target) as local_port:
            health_url = f"http://127.0.0.1:{local_port}/doc"
            status_code = _probe_health(local_port, cfg.agent.connect_timeout_seconds)
            data = data.model_copy(update={"reachable": 200 <= status_code < 400, "health_url": health_url, "health_status": status_code})
        if not data.reachable:
            raise AgentError("agent_unhealthy", f"agent returned HTTP {data.health_status}")
        op.finish(True)
        return Envelope.build(AGENT_STATUS_SCHEMA, data)
    except AgentError as exc:
        op.emit("failed", str(exc), level="error", code=exc.code, **exc.detail)
        op.finish(False)
        return Envelope.build(AGENT_STATUS_SCHEMA, data, [EnvelopeError(code=exc.code, message=str(exc), detail=exc.detail)])


def attach_agent(cfg: Config, host: str, session: str | None = None) -> int:
    """Run the native TUI in the inherited terminal and return its exit status."""
    target = resolve_agent_target(cfg, host)
    with open_tunnel(cfg, target) as local_port:
        _probe_health(local_port, cfg.agent.connect_timeout_seconds)
        command = ["opencode", "attach", f"http://127.0.0.1:{local_port}", "--dir", str(target.workdir)]
        if session:
            command.extend(["--session", session])
        try:
            return subprocess.run(command, check=False).returncode
        except OSError as exc:
            raise AgentError("opencode_client_unavailable", f"could not start controller opencode client: {exc}") from exc
