"""Shared, shell-free Ansible execution and inventory helpers."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field

from nctl_core.artifacts import OperationArtifacts


class AnsibleRunResult(BaseModel):
    mode: str
    command: list[str] = Field(default_factory=list)
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    recap: dict[str, dict[str, int]] = Field(default_factory=dict)
    failed_hosts: list[str] = Field(default_factory=list)
    unreachable_hosts: list[str] = Field(default_factory=list)
    timed_out: bool = False
    stdout_path: str = ""
    stderr_path: str = ""


CommandRunner = Callable[[list[str], Path, float | None], subprocess.CompletedProcess[str]]


class AnsibleRunner:
    def __init__(
        self,
        cwd: Path,
        *,
        timeout_seconds: float | None = None,
        artifacts: OperationArtifacts | None = None,
        command_runner: CommandRunner | None = None,
    ) -> None:
        self.cwd = cwd
        self.timeout_seconds = timeout_seconds
        self.artifacts = artifacts
        self.command_runner = command_runner or _run_command

    def run(self, args: list[str], *, mode: str, artifact_stem: str | None = None) -> AnsibleRunResult:
        sanitized = sanitize_command(args)
        try:
            completed = self.command_runner(args, self.cwd, self.timeout_seconds)
            result = AnsibleRunResult(
                mode=mode,
                command=sanitized,
                exit_code=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
                recap=parse_recap(completed.stdout),
            )
        except subprocess.TimeoutExpired as exc:
            result = AnsibleRunResult(
                mode=mode,
                command=sanitized,
                exit_code=124,
                stdout=_timeout_text(exc.stdout),
                stderr=_timeout_text(exc.stderr) or f"command timed out after {self.timeout_seconds} seconds",
                timed_out=True,
            )
        except OSError as exc:
            result = AnsibleRunResult(
                mode=mode,
                command=sanitized,
                exit_code=1,
                stderr=str(exc),
            )

        result.failed_hosts = sorted(host for host, counts in result.recap.items() if counts.get("failed", 0) > 0)
        result.unreachable_hosts = sorted(
            host for host, counts in result.recap.items() if counts.get("unreachable", 0) > 0
        )
        if artifact_stem and self.artifacts is not None:
            stdout_path = self.artifacts.write_text(f"{artifact_stem}.stdout", result.stdout)
            stderr_path = self.artifacts.write_text(f"{artifact_stem}.stderr", result.stderr)
            result.stdout_path = str(stdout_path)
            result.stderr_path = str(stderr_path)
        return result


def load_inventory(
    inventory: Path,
    playbook_dir: Path,
    *,
    timeout_seconds: float | None = None,
    command_runner: CommandRunner | None = None,
) -> tuple[dict[str, Any], str | None]:
    runner = AnsibleRunner(
        playbook_dir,
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
    )
    result = runner.run(["ansible-inventory", "-i", str(inventory), "--list"], mode="inventory")
    if result.exit_code != 0:
        detail = result.stderr.strip() or "no stderr"
        return {}, f"ansible-inventory failed with exit code {result.exit_code}: {detail}"
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return {}, f"ansible-inventory returned invalid JSON: {exc}"
    if not isinstance(payload, dict):
        return {}, "ansible-inventory JSON root is not an object"
    return payload, None


def inventory_group_hosts(payload: dict[str, Any], group: str) -> set[str]:
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


def parse_recap(stdout: str) -> dict[str, dict[str, int]]:
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


_SENSITIVE_KEY = re.compile(r"(?i)(token|password|passwd|secret|api[_-]?key|credential)")


def sanitize_command(args: list[str]) -> list[str]:
    sanitized: list[str] = []
    redact_next = False
    for arg in args:
        if redact_next:
            sanitized.append("<redacted>")
            redact_next = False
            continue
        if arg in {"--vault-password-file", "--ask-vault-pass"}:
            sanitized.append(arg)
            if arg != "--ask-vault-pass":
                redact_next = True
            continue
        if "=" in arg:
            key, _value = arg.split("=", 1)
            if _SENSITIVE_KEY.search(key):
                sanitized.append(f"{key}=<redacted>")
                continue
        if ":" in arg and _SENSITIVE_KEY.search(arg):
            sanitized.append("<redacted>")
            continue
        sanitized.append(arg)
    return sanitized


def _run_command(args: list[str], cwd: Path, timeout: float | None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _timeout_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value
