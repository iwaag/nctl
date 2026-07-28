from __future__ import annotations

import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "module",
    [
        "nctl_core.compute.model",
        "nctl_core.compute.contract",
        "nctl_core.compute.collection",
        "nctl_core.drift.gap_status",
        "nctl_core.drift.interfaces",
        "nctl_core.drift.ip_ranges",
    ],
)
def test_pure_domain_modules_do_not_load_transport_or_cli(module: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import importlib, sys; "
                f"importlib.import_module({module!r}); "
                "forbidden = {'httpx', 'typer', 'nctl_core.nautobot', 'nctl_core.cli'}; "
                "raise SystemExit(bool(forbidden & set(sys.modules)))"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
