"""Presentation for `nctl agent status`."""

from nctl_core.agent import AgentStatusData
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
