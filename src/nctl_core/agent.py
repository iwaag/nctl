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
from nctl_core.agent_api import AgentApiError, OpenCodeClient, reply_text
from nctl_core.events import OperationLog
from nctl_core.hosts_intent import select_mdns_endpoint
from nctl_core.nautobot import NautobotClient, NautobotError
from nctl_core.output import Envelope, EnvelopeError
from nctl_core.sources.actual import ActualSnapshot, fetch_actual_snapshot
from nctl_core.sources.desired import DesiredNode, DesiredSnapshot, fetch_desired_snapshot
from nctl_core.ssh_enroll import SshStoreReadError, _resolve_node, load_managed_ssh_store
from nctl_core.ssh_trust import derive_host_key_alias

AGENT_STATUS_SCHEMA = "nctl.agent.status.v1"
AGENT_RUN_SCHEMA = "nctl.agent.run.v1"
AGENT_SEND_SCHEMA = "nctl.agent.send.v1"
AGENT_SESSIONS_SCHEMA = "nctl.agent.sessions.v1"
AGENT_ABORT_SCHEMA = "nctl.agent.abort.v1"


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


class AgentSessionData(BaseModel):
    session_id: str
    title: str = ""
    created_at: str | None = None
    updated_at: str | None = None


class AgentTaskData(BaseModel):
    operation_id: str = ""
    node_slug: str = ""
    session_id: str = ""
    model: str | None = None
    runtime_version: str = ""
    duration_seconds: float = 0.0
    outcome: str = ""
    reply: str = ""


class AgentSessionsData(BaseModel):
    node_slug: str = ""
    sessions: list[AgentSessionData] = []


class AgentAbortData(BaseModel):
    operation_id: str = ""
    node_slug: str = ""
    session_id: str = ""
    accepted: bool = False
    duration_seconds: float = 0.0
    outcome: str = ""


@dataclass(frozen=True)
class AgentTarget:
    node: DesiredNode
    endpoint: str
    ssh_port: int
    alias: str
    workdir: Path


def _workdir_from_os(cfg: Config, os_name: str | None) -> Path | None:
    """Map nodeutils/declared OS vocabulary to the OS-wide workspace setting."""
    normalized = os_name.strip().lower() if os_name else ""
    if normalized in {"darwin", "macos"}:
        return cfg.agent.macos_workdir
    if normalized == "linux":
        return cfg.agent.linux_workdir
    return None


def _target_from_snapshot(
    cfg: Config, snapshot: DesiredSnapshot, actual: ActualSnapshot, host: str
) -> AgentTarget:
    node, error = _resolve_node(snapshot, host)
    if error is not None or node is None:
        raise AgentError("unknown_host", f"no DesiredNode with slug {host!r}", {"host": host})
    endpoint = select_mdns_endpoint([item for item in snapshot.endpoints if item.node_id == node.id])
    if endpoint is None or not endpoint.mdns_name:
        raise AgentError("node_without_mdns", f"DesiredNode {host!r} has no mDNS endpoint", {"host": host})
    override = next((item for item in snapshot.operational_overrides if item.node_id == node.id), None)
    ssh_port = override.ansible_port if override and override.ansible_port else 22
    declared_os = override.declared_host_os if override else None
    devices_by_id = {device.id: device for device in actual.devices}
    device = devices_by_id.get(node.realized_device_id or "")
    observed_os = device.actual_facts().observed_system if device is not None else None
    workdir = _workdir_from_os(cfg, observed_os) or _workdir_from_os(cfg, declared_os)
    if workdir is None:
        raise AgentError(
            "agent_workdir_unresolved",
            f"DesiredNode {host!r} needs a Linux/Darwin nodeutils observation or declared_host_os",
            {"host": host, "observed_system": observed_os, "declared_host_os": declared_os},
        )
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
        desired = fetch_desired_snapshot(client)
        actual = fetch_actual_snapshot(client)
        return _target_from_snapshot(cfg, desired, actual, host)
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


def _api_error(exc: AgentApiError) -> AgentError:
    return AgentError(exc.code, exc.message, exc.detail)


def _session_data(item: dict[str, object]) -> AgentSessionData:
    return AgentSessionData(
        session_id=str(item.get("id", "")), title=str(item.get("title", "")),
        created_at=str(item["time"]["created"]) if isinstance(item.get("time"), dict) and item["time"].get("created") is not None else None,
        updated_at=str(item["time"]["updated"]) if isinstance(item.get("time"), dict) and item["time"].get("updated") is not None else None,
    )


def build_agent_sessions(cfg: Config, host: str) -> Envelope[AgentSessionsData]:
    data = AgentSessionsData(node_slug=host)
    try:
        target = resolve_agent_target(cfg, host)
        with open_tunnel(cfg, target) as local_port:
            _probe_health(local_port, cfg.agent.connect_timeout_seconds)
            with OpenCodeClient(f"http://127.0.0.1:{local_port}", timeout_seconds=cfg.agent.connect_timeout_seconds) as api:
                data.sessions = [_session_data(item) for item in api.list_sessions(str(target.workdir))]
        return Envelope.build(AGENT_SESSIONS_SCHEMA, data)
    except AgentApiError as exc:
        failure = _api_error(exc)
    except AgentError as exc:
        failure = exc
    return Envelope.build(AGENT_SESSIONS_SCHEMA, data, [EnvelopeError(code=failure.code, message=str(failure), detail=failure.detail)])


def _build_agent_task(cfg: Config, host: str, prompt: str, session_id: str | None) -> Envelope[AgentTaskData]:
    name, schema = ("agent send", AGENT_SEND_SCHEMA) if session_id else ("agent run", AGENT_RUN_SCHEMA)
    op = OperationLog.start(name, cfg.events.resolved_log_dir())
    data = AgentTaskData(operation_id=op.operation_id, node_slug=host, session_id=session_id or "", model=cfg.agent.default_model, runtime_version=cfg.agent.runtime_version)
    started = time.monotonic()
    try:
        target = resolve_agent_target(cfg, host)
        with open_tunnel(cfg, target) as local_port:
            _probe_health(local_port, cfg.agent.connect_timeout_seconds)
            with OpenCodeClient(f"http://127.0.0.1:{local_port}", timeout_seconds=cfg.agent.request_timeout_seconds) as api:
                if session_id is None:
                    session = api.create_session()
                    data.session_id = str(session["id"])
                    model = session.get("model")
                    if isinstance(model, dict):
                        data.model = "/".join(str(model.get(key, "")) for key in ("providerID", "modelID")).strip("/") or None
                op.emit("session_ready", "session ready", host=host, session_id=data.session_id)
                response = api.send_and_wait(data.session_id, str(target.workdir), prompt, cfg.agent.request_timeout_seconds)
        data.reply = reply_text(response)
        data.duration_seconds = round(time.monotonic() - started, 3)
        data.outcome = "completed"
        op.emit("completed", "agent reply received", host=host, session_id=data.session_id, prompt_length=len(prompt), reply_length=len(data.reply), duration_seconds=data.duration_seconds)
        op.finish(True)
        return Envelope.build(schema, data)
    except AgentApiError as exc:
        failure = _api_error(exc)
    except AgentError as exc:
        failure = exc
    data.duration_seconds = round(time.monotonic() - started, 3)
    data.outcome = "timed_out" if failure.code == "agent_timeout" else "failed"
    if not data.session_id and isinstance(failure.detail.get("session_id"), str):
        data.session_id = failure.detail["session_id"]
    op.emit("failed", str(failure), level="error", code=failure.code, host=host, session_id=data.session_id, prompt_length=len(prompt), duration_seconds=data.duration_seconds)
    op.finish(False)
    return Envelope.build(schema, data, [EnvelopeError(code=failure.code, message=str(failure), detail=failure.detail)])


def build_agent_run(cfg: Config, host: str, prompt: str) -> Envelope[AgentTaskData]:
    return _build_agent_task(cfg, host, prompt, None)


def build_agent_send(cfg: Config, host: str, session_id: str, prompt: str) -> Envelope[AgentTaskData]:
    return _build_agent_task(cfg, host, prompt, session_id)


def build_agent_abort(cfg: Config, host: str, session_id: str) -> Envelope[AgentAbortData]:
    op = OperationLog.start("agent abort", cfg.events.resolved_log_dir())
    data = AgentAbortData(operation_id=op.operation_id, node_slug=host, session_id=session_id)
    started = time.monotonic()
    try:
        target = resolve_agent_target(cfg, host)
        with open_tunnel(cfg, target) as local_port:
            _probe_health(local_port, cfg.agent.connect_timeout_seconds)
            with OpenCodeClient(f"http://127.0.0.1:{local_port}", timeout_seconds=cfg.agent.connect_timeout_seconds) as api:
                data.accepted = api.interrupt(session_id)
        data.duration_seconds = round(time.monotonic() - started, 3)
        data.outcome = "interrupted"
        op.emit("interrupted", "OpenCode accepted interrupt", host=host, session_id=session_id, duration_seconds=data.duration_seconds)
        op.finish(True)
        return Envelope.build(AGENT_ABORT_SCHEMA, data)
    except AgentApiError as exc:
        failure = _api_error(exc)
    except AgentError as exc:
        failure = exc
    data.duration_seconds = round(time.monotonic() - started, 3)
    data.outcome = "failed"
    op.emit("failed", str(failure), level="error", code=failure.code, host=host, session_id=session_id, duration_seconds=data.duration_seconds)
    op.finish(False)
    return Envelope.build(AGENT_ABORT_SCHEMA, data, [EnvelopeError(code=failure.code, message=str(failure), detail=failure.detail)])
