"""Top-level CLI surface contract for remove_unused_surfaces Phase 2 (plan.md Step 1/5.3).

Protects a failure mode distinct from any single command's own tests: deleting the
dashboard package while accidentally leaving a Typer command registration or lazy
import behind. `serve` was removed in Phase 1; `dashboard` is removed here.
"""

import typer.main
from typer.testing import CliRunner

import nctl_core.cli.main as main

runner = CliRunner()

RETAINED_COMMANDS = {
    "status",
    "actual",
    "drift",
    "relations",
    "workspaces",
    "reconcile",
    "prune",
    "lifecycle",
    "desired",
    "render",
    "apply",
    "ops",
    "braindump",
    "ssh",
    "session",
    "workflow-episode",
    "upload",
}


def test_registered_top_level_commands_are_exactly_the_retained_set():
    click_app = typer.main.get_command(main.app)
    assert set(click_app.commands) == RETAINED_COMMANDS


def test_help_lists_every_retained_command():
    result = runner.invoke(main.app, ["--help"])
    assert result.exit_code == 0
    for command in RETAINED_COMMANDS:
        assert command in result.stdout, f"missing retained command: {command}"


def test_serve_is_an_unknown_command_not_a_compatibility_path():
    result = runner.invoke(main.app, ["serve"])
    assert result.exit_code == 2
    assert "no such command" in result.stderr.lower() or "no such command" in result.stdout.lower()


def test_dashboard_is_an_unknown_command_not_a_compatibility_path():
    result = runner.invoke(main.app, ["dashboard"])
    assert result.exit_code == 2
    assert "no such command" in result.stderr.lower() or "no such command" in result.stdout.lower()
