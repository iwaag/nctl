"""Thin operator client for a Phase 0 desired-state batch document."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

from nctl_core.config import Config
from nctl_core.desired_write import submit_batch
from nctl_core.nautobot import NautobotClient


_ENVELOPE_HINT = """document must be a Phase 0 batch envelope: a mapping with an operations list \
(dry_run is optional here; the CLI sets it from --yes). Minimal partial-upsert template:
operations:
  - op: upsert
    kind: desired_endpoint
    key: {desired_node: NODE_SLUG, name: primary, endpoint_type: primary}
    values: {gateway_address: 192.168.50.1}"""


def apply_document(cfg: Config, filename: str, *, commit: bool) -> dict[str, Any]:
    text = sys.stdin.read() if filename == "-" else Path(filename).read_text(encoding="utf-8")
    document = yaml.safe_load(text)
    if not isinstance(document, dict) or "operations" not in document or not set(document) <= {"dry_run", "operations"}:
        raise ValueError(_ENVELOPE_HINT)
    document["dry_run"] = not commit
    client = NautobotClient(cfg.nautobot.url, cfg.nautobot.resolve_token())
    try:
        return submit_batch(client, list(document["operations"]), dry_run=not commit)
    finally:
        client.close()
