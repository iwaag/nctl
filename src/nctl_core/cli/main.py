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
from nctl_core.output import emit
from nctl_core.status import build_status, render_status_text

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


JsonOption = Annotated[bool, typer.Option("--json", help="Print the nctl.status.v1 envelope as JSON.")]


@app.command()
def status(config: ConfigOption = None, json_output: JsonOption = False) -> None:
    """Check Nautobot connectivity, nodeutils dumps freshness, and submodule state."""
    cfg = _load_config(config)
    envelope = build_status(cfg)
    emit(envelope, json_output, render_status_text)
    raise typer.Exit(EXIT_OK if envelope.ok else EXIT_FAILURE)


def main() -> None:
    app()


if __name__ == "__main__":
    sys.exit(main())
