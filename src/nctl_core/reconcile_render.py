"""Human-readable presentation for completed reconcile envelopes."""

from nctl_core.output import Envelope
from nctl_core.reconcile.results import ReconcileData


def render_reconcile_text(envelope: Envelope[ReconcileData]) -> str:
    data = envelope.data
    lines = [
        f"operation_id: {data.operation_id}",
        f"mode: {data.mode}",
        f"scope: {data.scope.label()}",
        f"state: {data.state}",
        f"event_log: {data.event_log_path}",
    ]
    if data.plan_path:
        lines.append(f"plan: {data.plan_path}")
    if data.final_drift_path:
        lines.append(f"final_drift: {data.final_drift_path}")
    status_line = " ".join(f"{status}={count}" for status, count in sorted(data.scope_summary.items()))
    lines.append(f"scope summary: {status_line}" if status_line else "scope summary: (no targets)")
    if data.manual_review:
        lines.append(f"manual_review: {len(data.manual_review)} finding(s)")
    if data.unsupported:
        lines.append(f"unsupported: {len(data.unsupported)} finding(s)")
    if data.ssh_preflight:
        by_status: dict[str, list[str]] = {}
        for entry in data.ssh_preflight:
            by_status.setdefault(entry["status"], []).append(entry["slug"])
        parts = [f"{status}=[{', '.join(sorted(slugs))}]" for status, slugs in sorted(by_status.items())]
        lines.append(f"ssh_preflight: {' '.join(parts)}")
    for round_summary in data.rounds:
        lines.append(f"round {round_summary.round}: {len(round_summary.actions)} action(s)")
        for action in round_summary.actions:
            marker = "ok" if action.success else "FAILED"
            lines.append(f"    [{marker}] {action.action_id} ({action.reconciler_id})")
    for error in envelope.errors:
        lines.append(f"error [{error.code}]: {error.message}")
    lines.append(f"ok: {envelope.ok}")
    return "\n".join(lines)
