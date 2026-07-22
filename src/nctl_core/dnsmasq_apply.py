"""Long-running orchestration for ``nctl apply dnsmasq``."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from nctl_core.ansible import (
    AnsibleRunResult,
    AnsibleRunner,
    inventory_group_hosts,
    load_inventory,
    parse_recap,
)
from nctl_core.artifacts import ArtifactError, OperationArtifacts
from nctl_core.config import Config
from nctl_core.dnsmasq_render import build_dnsmasq_render
from nctl_core.events import OperationLog
from nctl_core.inventory_trust import check_inventory_ssh_preflight, validate_inventory_trust_contract
from nctl_core.output import Envelope, EnvelopeError
from nctl_core.reconcile.ssh_preflight import (
    STATUS_MISMATCH,
    STATUS_READY,
    STATUS_UNENROLLED,
    STATUS_UNREACHABLE,
    SshPreflightEntry,
)
from nctl_core.ssh_enroll import SshProbeRunner, SshStoreReadError, default_ssh_probe_runner

APPLY_DNSMASQ_SCHEMA = "nctl.apply.dnsmasq.v2"
SETUP_PLAYBOOK = Path("playbooks/bootstrap/setup_dnsmasq.yml")
DEPLOY_PLAYBOOK = Path("playbooks/dnsmasq/deploy_dnsmasq_records.yml")
TARGET_GROUP = "dnsmasq_server"


class DnsmasqApplyData(BaseModel):
    operation_id: str
    mode: str
    artifact_path: str = ""
    event_log_path: str
    inventory_path: str = ""
    target_group: str = TARGET_GROUP
    target_hosts: list[str] = Field(default_factory=list)
    render_summary: dict[str, Any] = Field(default_factory=dict)
    content_sha256: str = ""
    ssh_preflight: list[dict[str, Any]] = Field(default_factory=list)
    setup: AnsibleRunResult | None = None
    ansible: AnsibleRunResult | None = None


def build_dnsmasq_apply(
    cfg: Config,
    apply_changes: bool = False,
    inventory: Path | None = None,
    probe: SshProbeRunner | None = None,
) -> Envelope[DnsmasqApplyData]:
    """Render an artifact, validate the SSH trust contract, and invoke the deploy playbook.

    ``inventory``, if given, overrides ``cfg.ansible.resolved_inventory(...)`` for this run only
    -- the bootstrap-time escape hatch (`nctl apply dnsmasq --inventory PATH`) for actuating
    against a freshly rendered `hosts_intent.yml` before any production inventory exists.
    `reconcile` never passes this; it always actuates against the production inventory it
    regenerates itself.

    ``probe`` is the injected `ssh-keyscan`/`ssh -G`/`ssh-keygen -F` boundary (fix_sshkey2 Step 4);
    tests must always supply a fake so this never touches the real network or the developer's own
    known_hosts files. Every target host -- the configured default inventory as much as an
    explicit ``--inventory`` -- goes through the same closed trust-contract validation and
    offered-key preflight before any Ansible process starts, in both dry-run and apply mode
    (dry-run performs the identical read-only preflight and never mutates trust).
    """
    probe = probe or default_ssh_probe_runner()
    op = OperationLog.start("apply dnsmasq", cfg.events.resolved_log_dir())
    mode = "apply" if apply_changes else "dry-run"
    data = DnsmasqApplyData(
        operation_id=op.operation_id,
        mode=mode,
        event_log_path=str(op.path),
    )

    try:
        artifacts = OperationArtifacts.create(cfg.events.resolved_log_dir(), op.operation_id)
    except ArtifactError as exc:
        return _failure(
            op,
            data,
            [EnvelopeError(code="artifact_write_failed", message=str(exc))],
            "operation artifact directory is not writable",
        )

    render = build_dnsmasq_render(cfg, operation_id=op.operation_id)
    if not render.ok:
        return _failure(op, data, render.errors, "dnsmasq render failed")

    try:
        artifact_path = artifacts.write_text("artifacts/dnsmasq-records.conf", render.data.conf)
    except ArtifactError as exc:
        return _failure(
            op,
            data,
            [EnvelopeError(code="artifact_write_failed", message=str(exc))],
            "dnsmasq artifact write failed",
        )

    data.artifact_path = str(artifact_path)
    data.render_summary = render.data.summary
    data.content_sha256 = render.data.content_sha256
    op.emit(
        "rendered", "dnsmasq configuration rendered",
        artifact_path=str(artifact_path), content_sha256=render.data.content_sha256,
    )

    resolved_inventory = (
        inventory.expanduser().resolve()
        if inventory is not None
        else cfg.ansible.resolved_inventory(cfg.source_path.parent)
    )

    validation_error = _validate_paths(cfg, data, resolved_inventory)
    if validation_error is not None:
        return _failure(op, data, [validation_error], validation_error.message)

    inventory_result, inventory_error = _load_inventory(cfg, resolved_inventory)
    if inventory_error is not None:
        return _failure(op, data, [inventory_error], inventory_error.message)

    target_hosts = sorted(inventory_group_hosts(inventory_result, TARGET_GROUP))
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

    # fix_sshkey2 Step 4 (bug #4): every target host -- the normally
    # configured inventory as much as an explicit --inventory override --
    # goes through the same closed SSH trust contract before Ansible starts.
    # A hand-written or stale inventory (including the configured default,
    # which previously bypassed this check entirely) is rejected exactly
    # like an untrusted --inventory always was.
    known_hosts_path = cfg.resolved_ssh_known_hosts_file()
    host_vars_by_host = {host: _inventory_host_vars(inventory_result, host) for host in target_hosts}

    contract_errors = [
        finding
        for host in target_hosts
        if (finding := validate_inventory_trust_contract(host_vars_by_host[host], host, known_hosts_path))
        is not None
    ]
    if contract_errors:
        error = EnvelopeError(
            code="dnsmasq_inventory_untrusted_host",
            message=(
                "inventory host(s) fail the closed SSH trust contract: "
                + "; ".join(f"{e.hostname} ({e.code})" for e in contract_errors)
                + "; only inventories generated by nctl render hosts-intent/production participate "
                "in the SSH trust contract"
            ),
            detail={"hosts": [{"host": e.hostname, "code": e.code, "message": str(e)} for e in contract_errors]},
        )
        return _failure(op, data, [error], error.message)

    try:
        preflight_entries = check_inventory_ssh_preflight(
            known_hosts_path, cfg.ssh.keyscan_timeout_seconds, target_hosts, host_vars_by_host, probe
        )
    except SshStoreReadError as exc:
        error = EnvelopeError(code="ssh_store_read_failed", message=str(exc))
        return _failure(op, data, [error], error.message)
    data.ssh_preflight = [entry.model_dump() for entry in preflight_entries]
    ssh_errors = _ssh_preflight_errors(preflight_entries)
    if ssh_errors:
        return _failure(op, data, ssh_errors, ssh_errors[0].message)

    playbook_dir = cfg.ansible.resolved_playbook_dir(cfg.source_path.parent)
    runner = AnsibleRunner(
        playbook_dir,
        timeout_seconds=cfg.reconcile.ansible_timeout_seconds,
        artifacts=artifacts,
    )

    setup_args = [
        "ansible-playbook",
        "-i",
        str(resolved_inventory),
        str(playbook_dir / SETUP_PLAYBOOK),
    ]
    if not apply_changes:
        setup_args.extend(["--check", "--diff"])

    if apply_changes:
        op.emit("setup_started", "dnsmasq daemon setup started", target_hosts=target_hosts)

    setup_result = runner.run(setup_args, mode=mode, artifact_stem="ansible/dnsmasq-setup")
    data.setup = setup_result
    if setup_result.exit_code != 0:
        code = "ansible_setup_failed" if mode == "apply" else "ansible_setup_dry_run_failed"
        message = (
            f"ansible-playbook daemon setup {mode} timed out after "
            f"{cfg.reconcile.ansible_timeout_seconds} seconds"
            if setup_result.timed_out
            else f"ansible-playbook daemon setup {mode} exited with code {setup_result.exit_code}"
        )
        error = EnvelopeError(
            code=code,
            message=message,
            detail={
                "exit_code": setup_result.exit_code,
                "recap": setup_result.recap,
                "timed_out": setup_result.timed_out,
            },
        )
        return _failure(op, data, [error], error.message)

    if apply_changes:
        op.emit("setup_completed", "dnsmasq daemon setup completed", exit_code=setup_result.exit_code, recap=setup_result.recap)
    else:
        op.emit(
            "setup_dry_run_completed",
            "dnsmasq daemon setup dry-run completed",
            exit_code=setup_result.exit_code,
            recap=setup_result.recap,
        )

    playbook_path = playbook_dir / DEPLOY_PLAYBOOK
    args = [
        "ansible-playbook",
        "-i",
        str(resolved_inventory),
        str(playbook_path),
        "-e",
        f"dnsmasq_records_src={artifact_path}",
    ]
    if not apply_changes:
        args.extend(["--check", "--diff"])

    if apply_changes:
        op.emit("apply_started", "dnsmasq apply started", target_hosts=target_hosts)

    result = runner.run(args, mode=mode, artifact_stem="ansible/dnsmasq")
    data.ansible = result
    if result.exit_code != 0:
        code = "ansible_apply_failed" if mode == "apply" else "ansible_dry_run_failed"
        message = (
            f"ansible-playbook {mode} timed out after {cfg.reconcile.ansible_timeout_seconds} seconds"
            if result.timed_out
            else f"ansible-playbook {mode} exited with code {result.exit_code}"
        )
        error = EnvelopeError(
            code=code,
            message=message,
            detail={"exit_code": result.exit_code, "recap": result.recap, "timed_out": result.timed_out},
        )
        return _failure(op, data, [error], error.message)

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
    if data.setup is not None:
        lines.append("")
        lines.append("-- daemon setup --")
        if data.setup.stdout:
            lines.append(data.setup.stdout.rstrip())
        if data.setup.stderr:
            lines.append(data.setup.stderr.rstrip())
    if data.ansible is not None:
        lines.append("")
        lines.append("-- records deploy --")
        if data.ansible.stdout:
            lines.append(data.ansible.stdout.rstrip())
        if data.ansible.stderr:
            lines.append(data.ansible.stderr.rstrip())
    for error in envelope.errors:
        lines.append(f"error [{error.code}]: {error.message}")
    lines.append(f"ok: {envelope.ok}")
    return "\n".join(lines)


def _validate_paths(cfg: Config, data: DnsmasqApplyData, inventory: Path) -> EnvelopeError | None:
    playbook_dir = cfg.ansible.resolved_playbook_dir(cfg.source_path.parent)
    data.inventory_path = str(inventory)

    if not playbook_dir.is_dir():
        return EnvelopeError(
            code="ansible_playbook_dir_missing",
            message=f"ansible.playbook_dir does not exist or is not a directory: {playbook_dir}",
        )
    setup_playbook = playbook_dir / SETUP_PLAYBOOK
    if not setup_playbook.is_file():
        return EnvelopeError(code="ansible_playbook_missing", message=f"setup playbook not found: {setup_playbook}")
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


def _load_inventory(cfg: Config, inventory: Path) -> tuple[dict[str, Any], EnvelopeError | None]:
    playbook_dir = cfg.ansible.resolved_playbook_dir(cfg.source_path.parent)
    payload, error = load_inventory(
        inventory,
        playbook_dir,
        timeout_seconds=cfg.reconcile.ansible_timeout_seconds,
    )
    if error is None:
        return payload, None
    code = (
        "ansible_inventory_invalid"
        if "invalid JSON" in error or "JSON root" in error
        else "ansible_inventory_failed"
    )
    return {}, EnvelopeError(code=code, message=error)


def _inventory_host_vars(payload: dict[str, Any], hostname: str) -> dict[str, Any]:
    meta = payload.get("_meta")
    if isinstance(meta, dict):
        hostvars = meta.get("hostvars")
        if isinstance(hostvars, dict):
            value = hostvars.get(hostname)
            if isinstance(value, dict):
                return value
    return {}


_PREFLIGHT_STATUS_CODES = (
    (STATUS_UNENROLLED, "ssh_host_key_unenrolled"),
    (STATUS_MISMATCH, "ssh_host_key_mismatch"),
    (STATUS_UNREACHABLE, "ssh_host_key_unreachable"),
)


def _ssh_preflight_errors(entries: list[SshPreflightEntry]) -> list[EnvelopeError]:
    """Turn non-ready `check_inventory_ssh_preflight` entries into structured envelope errors.

    Distinguishes `ssh_host_key_unenrolled` from `ssh_host_key_mismatch` from
    `ssh_host_key_unreachable` (fix_sshkey2 Step 4 item 7) -- and from the
    separate `dnsmasq_inventory_untrusted_host` contract-validation error,
    which is raised earlier and never reaches this function.
    """
    bad = [entry for entry in entries if entry.status != STATUS_READY]
    errors: list[EnvelopeError] = []
    for status, code in _PREFLIGHT_STATUS_CODES:
        matching = [entry for entry in bad if entry.status == status]
        if matching:
            hosts = ", ".join(sorted(entry.slug for entry in matching))
            errors.append(
                EnvelopeError(
                    code=code,
                    message=f"{code}: {hosts}",
                    detail={"hosts": [entry.model_dump() for entry in matching]},
                )
            )
    return errors


# Compatibility aliases for callers/tests that used the pre-Step-1 private names.
_inventory_group_hosts = inventory_group_hosts
_parse_recap = parse_recap


def _failure(
    op: OperationLog,
    data: DnsmasqApplyData,
    errors: list[EnvelopeError],
    message: str,
) -> Envelope[DnsmasqApplyData]:
    op.emit("failed", message, level="error", error_codes=[error.code for error in errors])
    op.finish(ok=False)
    return Envelope.build(APPLY_DNSMASQ_SCHEMA, data, errors)
