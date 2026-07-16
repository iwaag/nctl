"""Staged-validation atomic write for rendered Ansible inventories (Phase 1.5 Step 3).

Factored out of `production_render` so `render production` and
`render hosts-intent` share one mechanism: write the YAML to a staged sibling
file, validate it with `ansible-inventory --list`, and only then atomically
replace the real path. A failed validation leaves the previous file untouched,
preserving the rescue semantics of the retired export playbooks.
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path

from nctl_core.output import EnvelopeError


def write_validated_inventory(inventory_yaml: str, inventory_path: Path) -> EnvelopeError | None:
    if shutil.which("ansible-inventory") is None:
        return EnvelopeError(
            code="ansible_executable_missing", message="ansible-inventory must be available on PATH"
        )

    out_dir = inventory_path.parent
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return EnvelopeError(code="artifact_write_failed", message=f"cannot create {out_dir}: {exc}")

    staged_path = out_dir / f".{inventory_path.name}.{uuid.uuid4()}.tmp"
    try:
        staged_path.write_text(inventory_yaml)
    except OSError as exc:
        return EnvelopeError(code="artifact_write_failed", message=f"cannot write {staged_path}: {exc}")

    try:
        completed = subprocess.run(
            ["ansible-inventory", "-i", str(staged_path), "--list"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        staged_path.unlink(missing_ok=True)
        return EnvelopeError(code="ansible_inventory_failed", message=f"cannot run ansible-inventory: {exc}")

    if completed.returncode != 0:
        staged_path.unlink(missing_ok=True)
        return EnvelopeError(
            code="ansible_inventory_invalid",
            message=f"ansible-inventory --list rejected the rendered inventory: {completed.stderr.strip()}",
        )

    try:
        staged_path.replace(inventory_path)
    except OSError as exc:
        staged_path.unlink(missing_ok=True)
        return EnvelopeError(
            code="artifact_write_failed", message=f"cannot replace {inventory_path}: {exc}"
        )
    return None
