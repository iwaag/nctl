"""Private, atomic artifacts for one long-running nctl operation."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class ArtifactError(RuntimeError):
    """An operation artifact path is unsafe or cannot be written."""


class OperationArtifacts:
    """Write mode-0600 files below a mode-0700 operation directory."""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()

    @classmethod
    def create(cls, events_dir: Path, operation_id: str) -> "OperationArtifacts":
        artifacts = cls(events_dir / operation_id)
        artifacts.ensure_writable()
        return artifacts

    def ensure_writable(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.root, 0o700)
            probe = self.root / ".write-probe"
            self._atomic_write(probe, b"")
            probe.unlink()
        except OSError as exc:
            raise ArtifactError(f"cannot establish operation artifact directory {self.root}: {exc}") from exc

    def path(self, relative: str | Path) -> Path:
        relative_path = Path(relative)
        if relative_path.is_absolute():
            raise ArtifactError(f"artifact path must be relative: {relative_path}")
        candidate = (self.root / relative_path).resolve()
        if not candidate.is_relative_to(self.root):
            raise ArtifactError(f"artifact path escapes operation directory: {relative_path}")
        return candidate

    def write_text(self, relative: str | Path, content: str) -> Path:
        destination = self.path(relative)
        try:
            self._atomic_write(destination, content.encode("utf-8"))
        except OSError as exc:
            raise ArtifactError(f"cannot write operation artifact {destination}: {exc}") from exc
        return destination

    def write_json(self, relative: str | Path, payload: Any) -> Path:
        return self.write_text(relative, json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=True) + "\n")

    def directory(self, relative: str | Path) -> Path:
        destination = self.path(relative)
        try:
            destination.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(destination, 0o700)
        except OSError as exc:
            raise ArtifactError(f"cannot create operation artifact directory {destination}: {exc}") from exc
        return destination

    def _atomic_write(self, destination: Path, content: bytes) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(destination.parent, 0o700)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
        temporary = Path(temporary_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            os.chmod(destination, 0o600)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            temporary.unlink(missing_ok=True)
            raise


def atomic_write_private(destination: Path, content: bytes) -> Path:
    """Atomically replace an arbitrary private file, including fsync and mode hardening."""

    destination = destination.expanduser().resolve()
    try:
        OperationArtifacts(destination.parent)._atomic_write(destination, content)
    except OSError as exc:
        raise ArtifactError(f"cannot atomically write private file {destination}: {exc}") from exc
    return destination
