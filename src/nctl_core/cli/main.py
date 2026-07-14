"""nctl CLI: thin Typer wrappers around nctl_core.

Convention: commands parse arguments, call nctl_core, and render the result.
No business logic lives in this module.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated, Optional

import typer

from nctl_core.config import Config, ConfigError

app = typer.Typer(help="Unified CLI for pj-clusterintent reconciliation workflows.")


@app.callback()
def _root() -> None:
    """Keep subcommand names explicit even while only one command exists."""

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2

ConfigOption = Annotated[
    Optional[Path],
    typer.Option("--config", help="Path to nctl.toml (defaults to $NCTL_CONFIG, ./nctl.toml, repo root)."),
]


def _load_config(config_path: Path | None) -> Config:
    try:
        return Config.load(config_path)
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(EXIT_USAGE)


@app.command()
def status(config: ConfigOption = None) -> None:
    """Show cluster tooling status (Phase 0 stub: config resolution only)."""
    cfg = _load_config(config)
    typer.echo(f"config:      {cfg.source_path}")
    typer.echo(f"nautobot:    {cfg.nautobot.url}")
    typer.echo(f"dumps dir:   {cfg.inventory.resolved_dumps_dir()}")
    typer.echo(f"events dir:  {cfg.events.resolved_log_dir()}")
    typer.echo(f"repo root:   {cfg.repo_root()}")
    typer.echo("checks:      not implemented yet (Steps 0.3-0.6)")


def main() -> None:
    app()


if __name__ == "__main__":
    sys.exit(main())
