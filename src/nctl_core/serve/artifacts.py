"""Public, allowlisted view of operation artifacts."""

from __future__ import annotations

import re
import stat
from pathlib import Path

from pydantic import BaseModel

from nctl_core.operations_index import OperationRecord

_ALLOWED = (
    re.compile(r"^(?:plan|result|drift)\.json$"),
    re.compile(r"^round-\d{2}/drift-(?:before|final)\.json$"),
)
_DENIED_PARTS = {"reports", "probe-config", "slurp", "jobs", "ansible"}


class PublicArtifact(BaseModel):
    name: str
    size_bytes: int


def list_public_artifacts(record: OperationRecord) -> list[PublicArtifact]:
    result: list[PublicArtifact] = []
    for artifact in record.artifacts:
        path = resolve_public_artifact(record, artifact.name)
        if path is not None:
            result.append(PublicArtifact(name=artifact.name, size_bytes=artifact.size_bytes))
    return result


def resolve_public_artifact(record: OperationRecord, name: str) -> Path | None:
    if record.artifact_dir is None or not _name_allowed(name):
        return None
    unresolved_root = Path(record.artifact_dir)
    if unresolved_root.is_symlink():
        return None
    root = unresolved_root.resolve()
    relative = Path(name)
    candidate = root / relative
    try:
        # Reject every symlink component before resolving it.
        current = root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                return None
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(root) or not resolved.is_file():
            return None
        mode = resolved.stat().st_mode
    except OSError:
        return None
    # Mode 0600 is the repository's explicit marker for private operation data.
    if stat.S_IMODE(mode) == 0o600:
        return None
    return resolved


def _name_allowed(name: str) -> bool:
    relative = Path(name)
    if relative.is_absolute() or not relative.parts or any(part in ("", ".", "..") for part in relative.parts):
        return False
    if any(part in _DENIED_PARTS for part in relative.parts):
        return False
    return any(pattern.fullmatch(relative.as_posix()) for pattern in _ALLOWED)
