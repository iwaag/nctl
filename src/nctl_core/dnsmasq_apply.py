"""Long-running orchestration for ``nctl apply dnsmasq``."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from nctl_core.config import Config
from nctl_core.dnsmasq_render import build_dnsmasq_render
from nctl_core.events import OperationLog
from nctl_core.output import Envelope, EnvelopeError

APPLY_DNSMASQ_SCHEMA = "nctl.apply.dnsmasq.v1"
DEPLOY_PLAYBOOK = Path("playbooks/dnsmasq/deploy_dnsmasq_records.yml")
TARGET_GROUP = "dnsmasq_server"


class AnsibleRunResult(BaseModel):
    mode: str
    command: list[str] = Field(default_factory=list)
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    recap: dict[str, dict[str, int]] = Field(default_factory=dict)


class DnsmasqApplyData(BaseModel):
    operation_id: str
    mode: str
    artifact_path: str = ""
    event_log_path: str
    inventory_path: str = ""
    target_group: str = TARGET_GROUP
    target_hosts: list[str] = Field(default_factory=list)
    render_summary: dict[str, Any] = Field(default_factory=dict)
    ansible: AnsibleRunResult | None = None


def build_dnsmasq_apply(cfg: Config, apply_changes: bool = False) -> Envelope[DnsmasqApplyData]:
    """Render an artifact, validate inventory targets, and invoke the deploy playbook."""
    op = OperationLog.start("apply dnsmasq", cfg.events.resolved_log_dir())
    mode = "apply" if apply_changes else "dry-run"
    data = DnsmasqApplyData(
        operation_id=op.operation_id,
        mode=mode,
        event_log_path=str(op.path),
    )

    render = build_dnsmasq_render(cfg, operation_id=op.operation_id)
    if not render.ok:
        return _failure(op, data, render.errors, "dnsmasq render failed")

    artifact_path = (
        cfg.events.resolved_log_dir() / op.operation_id / "artifacts" / "dnsmasq-records.conf"
    )
    try:
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(render.data.conf)
    except OSError as exc:
        return _failure(
            op,
            data,
            [EnvelopeError(code="artifact_write_failed", message=f"cannot write {artifact_path}: {exc}")],
            "dnsmasq artifact write failed",
        )

    data.artifact_path = str(artifact_path)
    data.render_summary = render.data.summary
    op.emit("rendered", "dnsmasq configuration rendered", artifact_path=str(artifact_path))

    validation_error = _validate_paths(cfg, data)
    if validation_error is not None:
        return _failure(op, data, [validation_error], validation_error.message)

    inventory_result, inventory_error = _load_inventory(cfg)
    if inventory_error is not None:
        return _failure(op, data, [inventory_error], inventory_error.message)

    target_hosts = sorted(_inventory_group_hosts(inventory_result, TARGET_GROUP))
    data.target_hosts = target_hosts
    if not target_hosts:
        error = EnvelopeError(
            code="dnsmasq_inventory_group_empty",
            message=(
                f"configured inventory has no hosts in {TARGET_GROUP!r}: {data.inventory_path}; "
                "generate or select a deployment inventory that defines the dnsmasq target"
            ),
        )
        return _failure(op, data, [error], error.message)

    playbook_dir = cfg.ansible.resolved_playbook_dir(cfg.source_path.parent)
    playbook_path = playbook_dir / DEPLOY_PLAYBOOK
    args = [
        "ansible-playbook",
        "-i",
        str(cfg.ansible.resolved_inventory(cfg.source_path.parent)),
        str(playbook_path),
        "-e",
        f"dnsmasq_records_src={artifact_path}",
    ]
    if not apply_changes:
        args.extend(["--check", "--diff"])

    if apply_changes:
        op.emit("apply_started", "dnsmasq apply started", target_hosts=target_hosts)

    result, run_error = _run_ansible(args, playbook_dir, mode)
    data.ansible = result
    if run_error is not None:
        return _failure(op, data, [run_error], run_error.message)

    if apply_changes:
        op.emit("apply_completed", "dnsmasq apply completed", exit_code=result.exit_code, recap=result.recap)
    else:
        op.emit("dry_run_completed", "dnsmasq dry-run completed", exit_code=result.exit_code, recap=result.recap)
    op.finish(ok=True)
    return Envelope.build(APPLY_DNSMASQ_SCHEMA, data, [])


def render_dnsmasq_apply_text(envelope: Envelope[DnsmasqApplyData]) -> str:
    data = envelope.data
    lines = [
        f"operation_id: {data.operation_id}",
        f"mode: {data.mode}",
        f"artifact: {data.artifact_path or '-'}",
        f"event_log: {data.event_log_path}",
    ]
    if data.target_hosts:
        lines.append(f"targets: {', '.join(data.target_hosts)}")
    if data.ansible is not None:
        if data.ansible.stdout:
            lines.extend(["", data.ansible.stdout.rstrip()])
        if data.ansible.stderr:
            lines.extend(["", data.ansible.stderr.rstrip()])
    for error in envelope.errors:
        lines.append(f"error [{error.code}]: {error.message}")
    lines.append(f"ok: {envelope.ok}")
    return "\n".join(lines)


def _validate_paths(cfg: Config, data: DnsmasqApplyData) -> EnvelopeError | None:
    config_dir = cfg.source_path.parent
    playbook_dir = cfg.ansible.resolved_playbook_dir(config_dir)
    inventory = cfg.ansible.resolved_inventory(config_dir)
    data.inventory_path = str(inventory)

    if not playbook_dir.is_dir():
        return EnvelopeError(
            code="ansible_playbook_dir_missing",
            message=f"ansible.playbook_dir does not exist or is not a directory: {playbook_dir}",
        )
    playbook = playbook_dir / DEPLOY_PLAYBOOK
    if not playbook.is_file():
        return EnvelopeError(code="ansible_playbook_missing", message=f"deploy playbook not found: {playbook}")
    if not inventory.exists():
        return EnvelopeError(
            code="ansible_inventory_missing",
            message=(
                f"ansible.inventory does not exist: {inventory}; generate the inventory first "
                "with `nctl render production --out <inventory-directory>`"
            ),
        )
    if shutil.which("ansible-inventory") is None or shutil.which("ansible-playbook") is None:
        return EnvelopeError(
            code="ansible_executable_missing",
            message="ansible-inventory and ansible-playbook must both be available on PATH",
        )
    return None


def _load_inventory(cfg: Config) -> tuple[dict[str, Any], EnvelopeError | None]:
    playbook_dir = cfg.ansible.resolved_playbook_dir(cfg.source_path.parent)
    inventory = cfg.ansible.resolved_inventory(cfg.source_path.parent)
    try:
        completed = _run_command(["ansible-inventory", "-i", str(inventory), "--list"], playbook_dir)
    except OSError as exc:
        return {}, EnvelopeError(code="ansible_inventory_failed", message=f"cannot run ansible-inventory: {exc}")
    if completed.returncode != 0:
        return {}, EnvelopeError(
            code="ansible_inventory_failed",
            message=f"ansible-inventory failed with exit code {completed.returncode}: {completed.stderr.strip()}",
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return {}, EnvelopeError(code="ansible_inventory_invalid", message=f"ansible-inventory returned invalid JSON: {exc}")
    if not isinstance(payload, dict):
        return {}, EnvelopeError(code="ansible_inventory_invalid", message="ansible-inventory JSON root is not an object")
    return payload, None


def _inventory_group_hosts(payload: dict[str, Any], group: str) -> set[str]:
    seen: set[str] = set()

    def visit(name: str) -> set[str]:
        if name in seen:
            return set()
        seen.add(name)
        value = payload.get(name)
        if not isinstance(value, dict):
            return set()
        hosts_value = value.get("hosts", [])
        if isinstance(hosts_value, list):
            hosts = set(hosts_value)
        elif isinstance(hosts_value, dict):
            hosts = set(hosts_value)
        else:
            hosts = set()
        children = value.get("children", [])
        if isinstance(children, list):
            child_names = children
        elif isinstance(children, dict):
            child_names = list(children)
        else:
            child_names = []
        for child in child_names:
            hosts.update(visit(str(child)))
        return {str(host) for host in hosts}

    return visit(group)


def _run_ansible(
    args: list[str],
    cwd: Path,
    mode: str,
) -> tuple[AnsibleRunResult, EnvelopeError | None]:
    try:
        completed = _run_command(args, cwd)
    except OSError as exc:
        result = AnsibleRunResult(mode=mode, command=args, exit_code=1, stderr=str(exc))
        return result, EnvelopeError(code="ansible_run_failed", message=f"cannot run ansible-playbook: {exc}")

    result = AnsibleRunResult(
        mode=mode,
        command=args,
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        recap=_parse_recap(completed.stdout),
    )
    if completed.returncode != 0:
        code = "ansible_apply_failed" if mode == "apply" else "ansible_dry_run_failed"
        return result, EnvelopeError(
            code=code,
            message=f"ansible-playbook {mode} exited with code {completed.returncode}",
            detail={"exit_code": completed.returncode, "recap": result.recap},
        )
    return result, None


def _run_command(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)


def _parse_recap(stdout: str) -> dict[str, dict[str, int]]:
    recap: dict[str, dict[str, int]] = {}
    for line in stdout.splitlines():
        match = re.match(r"^(\S+)\s*:\s*(.*)$", line.strip())
        if not match:
            continue
        counts = {
            key: int(value)
            for key, value in re.findall(
                r"(ok|changed|unreachable|failed|skipped|rescued|ignored)=(\d+)",
                match.group(2),
            )
        }
        if counts:
            recap[match.group(1)] = counts
    return recap


def _failure(
    op: OperationLog,
    data: DnsmasqApplyData,
    errors: list[EnvelopeError],
    message: str,
) -> Envelope[DnsmasqApplyData]:
    op.emit("failed", message, level="error", error_codes=[error.code for error in errors])
    op.finish(ok=False)
    return Envelope.build(APPLY_DNSMASQ_SCHEMA, data, errors)
