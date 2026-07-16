"""Read nodeutils inventory dumps (`schema_version: nodeutils.inventory.v1`)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

EXPECTED_SCHEMA_VERSION = "nodeutils.inventory.v1"


class DumpError(Exception):
    """A dump file is missing, unparsable, or has an unsupported schema_version."""


class NodeIdentity(BaseModel):
    model_config = ConfigDict(extra="allow")

    hostname: str


class NodeDump(BaseModel):
    """Only the fields Phase 0 needs; `facts`/`self_reported` stay raw (Phase 2 owns their typing)."""

    schema_version: str
    collector: Any = None
    identity: NodeIdentity
    collected_at: datetime
    facts: dict[str, Any] = {}
    self_reported: dict[str, Any] = {}


class DumpScanResult(BaseModel):
    dumps: list[NodeDump]
    errors: list[str]


def load_dump(path: Path) -> NodeDump:
    try:
        text = path.read_text()
    except OSError as exc:
        raise DumpError(f"cannot read {path}: {exc}") from exc

    return parse_dump_text(text, source=str(path), suffix=path.suffix)


def parse_dump_text(text: str, *, source: str = "report", suffix: str = ".json") -> NodeDump:
    try:
        if suffix == ".json":
            raw = json.loads(text)
        else:
            raw = yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise DumpError(f"cannot parse {source}: {exc}") from exc

    try:
        dump = NodeDump.model_validate(raw)
    except ValidationError as exc:
        raise DumpError(f"invalid dump {source}: {exc}") from exc

    if dump.schema_version != EXPECTED_SCHEMA_VERSION:
        raise DumpError(
            f"{source}: unsupported schema_version {dump.schema_version!r} "
            f"(expected {EXPECTED_SCHEMA_VERSION!r})"
        )
    return dump


def scan_dumps(dumps_dir: Path) -> DumpScanResult:
    """Discover *.json/*.yaml/*.yml reports; one bad file doesn't stop the scan."""
    if not dumps_dir.is_dir():
        return DumpScanResult(dumps=[], errors=[f"dumps dir not found: {dumps_dir}"])

    paths = sorted(
        p
        for pattern in ("*.json", "*.yaml", "*.yml")
        for p in dumps_dir.glob(pattern)
    )

    dumps: list[NodeDump] = []
    errors: list[str] = []
    for path in paths:
        try:
            dumps.append(load_dump(path))
        except DumpError as exc:
            errors.append(str(exc))
    return DumpScanResult(dumps=dumps, errors=errors)
