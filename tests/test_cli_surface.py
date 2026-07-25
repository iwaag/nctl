"""Top-level CLI surface contract for remove_unused_surfaces Phase 1 (plan.md Step 1/5.3).

Protects a failure mode distinct from any single command's own tests: deleting the
server package while accidentally leaving a Typer command registration or lazy
import behind. `dashboard` is intentionally retained until Phase 2.
"""

from typer.testing import CliRunner

import nctl_core.cli.main as main

runner = CliRunner()

RETAINED_COMMANDS = {
    "status",
    "actual",
    "drift",
    "dashboard",
    "reconcile",
    "lifecycle",
    "render",
    "apply",
    "ops",
    "braindump",
    "ssh",
    "session",
}


def test_help_lists_exactly_the_retained_commands_and_no_serve():
    result = runner.invoke(main.app, ["--help"])
    assert result.exit_code == 0
    for command in RETAINED_COMMANDS:
        assert command in result.stdout, f"missing retained command: {command}"
    assert "serve" not in result.stdout


def test_serve_is_an_unknown_command_not_a_compatibility_path():
    result = runner.invoke(main.app, ["serve"])
    assert result.exit_code == 2
    assert "no such command" in result.stderr.lower() or "no such command" in result.stdout.lower()
