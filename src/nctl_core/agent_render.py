"""Presentation for node-agent envelopes."""

from nctl_core.agent import AgentAbortData, AgentSessionsData, AgentStatusData, AgentTaskData
from nctl_core.output import Envelope


def render_agent_status_text(envelope: Envelope[AgentStatusData]) -> str:
    data = envelope.data
    lines = [f"{'✓' if data.reachable else '✗'} agent {data.node_slug}"]
    if data.endpoint:
        lines.append(f"    endpoint: {data.endpoint}:{data.ssh_port} -> 127.0.0.1:{data.agent_port}")
    if data.workdir:
        lines.append(f"    workdir: {data.workdir}")
    if data.health_status is not None:
        lines.append(f"    health: HTTP {data.health_status}")
    lines.extend(f"error [{error.code}]: {error.message}" for error in envelope.errors)
    lines.append(f"ok: {envelope.ok}")
    return "\n".join(lines)


def render_agent_task_text(envelope: Envelope[AgentTaskData]) -> str:
    data = envelope.data
    lines = [f"{'✓' if envelope.ok else '✗'} agent {data.node_slug} session {data.session_id}"]
    if data.reply:
        lines.extend(["", data.reply])
    lines.extend(f"error [{error.code}]: {error.message}" for error in envelope.errors)
    lines.append(f"ok: {envelope.ok}")
    return "\n".join(lines)


def render_agent_sessions_text(envelope: Envelope[AgentSessionsData]) -> str:
    lines = [f"agent sessions {envelope.data.node_slug}"]
    lines.extend(f"{item.session_id}  {item.title}" for item in envelope.data.sessions)
    lines.extend(f"error [{error.code}]: {error.message}" for error in envelope.errors)
    lines.append(f"ok: {envelope.ok}")
    return "\n".join(lines)


def render_agent_abort_text(envelope: Envelope[AgentAbortData]) -> str:
    data = envelope.data
    lines = [f"{'✓' if envelope.ok else '✗'} agent abort {data.node_slug} session {data.session_id}"]
    lines.extend(f"error [{error.code}]: {error.message}" for error in envelope.errors)
    lines.append(f"ok: {envelope.ok}")
    return "\n".join(lines)
