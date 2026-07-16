"""`nctl render hosts-intent`: fetch + render as one synchronous call (Phase 1.5 Steps 2-3).

Fast/synchronous like the other render commands — no operation ID or event
log. The fetch reuses the Phase 2 pinned desired-state query
(`sources.desired.fetch_desired_snapshot`); the renderer only needs nodes and
endpoints, and desired-only is one GraphQL round trip (no actual-side fetch),
so this does not go through `build_source_snapshot`.

Command name: the roadmap says `render inventory`, named before Phase 2
introduced `render production` (also an inventory). `hosts-intent` matches the
artifact name and the bootstrap-vs-production vocabulary in ansible_agdev.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from nctl_core.config import Config, ConfigError
from nctl_core.hosts_intent import (
    export_hosts_intent,
    hosts_intent_payload,
    render_hosts_intent_json,
    render_hosts_intent_yml,
)
from nctl_core.inventory_write import write_validated_inventory
from nctl_core.nautobot import NautobotClient, NautobotError
from nctl_core.output import Envelope, EnvelopeError
from nctl_core.sources.desired import fetch_desired_snapshot

RENDER_HOSTS_INTENT_SCHEMA = "nctl.render.hosts_intent.v1"

INVENTORY_FILENAME = "hosts_intent.yml"
EXPORT_JSON_FILENAME = "hosts-intent-export.json"


class HostsIntentRenderData(BaseModel):
    schema_version: str = ""
    summary: dict[str, Any] = {}
    inventory: dict[str, Any] = {}
    hosts: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    inventory_yaml: str = ""
    export_json: str = ""


def build_hosts_intent_render(cfg: Config) -> Envelope[HostsIntentRenderData]:
    generated_at = datetime.now(timezone.utc).isoformat()

    try:
        token = cfg.nautobot.resolve_token()
    except ConfigError as exc:
        return _failed(EnvelopeError(code="nautobot_token_error", message=str(exc)))

    client = NautobotClient(cfg.nautobot.url, token)
    try:
        snapshot = fetch_desired_snapshot(client)
    except NautobotError as exc:
        return _failed(EnvelopeError(code="nautobot_fetch_failed", message=str(exc)))
    finally:
        client.close()

    export = export_hosts_intent(snapshot.nodes, snapshot.endpoints)
    payload = hosts_intent_payload(export, generated_at=generated_at)
    data = HostsIntentRenderData(
        schema_version=payload["schema_version"],
        summary=payload["summary"],
        inventory=payload["inventory"],
        hosts=payload["hosts"],
        skipped=payload["skipped"],
        inventory_yaml=render_hosts_intent_yml(export, generated_at=generated_at),
        export_json=render_hosts_intent_json(export, generated_at=generated_at),
    )
    return Envelope.build(RENDER_HOSTS_INTENT_SCHEMA, data, [])


def render_hosts_intent_inventory_text(envelope: Envelope[HostsIntentRenderData]) -> str:
    """The inventory YAML itself in the success case; error lines otherwise."""
    if not envelope.ok:
        return _error_text(envelope)
    return envelope.data.inventory_yaml


def render_hosts_intent_summary_text(envelope: Envelope[HostsIntentRenderData]) -> str:
    """Human summary for the `--out` case, where the inventory went to a file."""
    if not envelope.ok:
        return _error_text(envelope)
    summary = envelope.data.summary
    return "\n".join(
        f"{key}: {summary.get(key)}"
        for key in ("total_nodes", "exported_hosts", "skipped_nodes", "groups")
    )


def write_hosts_intent_artifacts(
    envelope: Envelope[HostsIntentRenderData], out_dir: Path
) -> EnvelopeError | None:
    """Validate + atomically replace `<out_dir>/hosts_intent.yml` and write the
    companion `<out_dir>/hosts-intent-export.json`."""

    if not envelope.ok:
        return envelope.errors[0] if envelope.errors else EnvelopeError(code="render_failed", message="render failed")

    data = envelope.data
    error = write_validated_inventory(data.inventory_yaml, out_dir / INVENTORY_FILENAME)
    if error is not None:
        return error

    try:
        (out_dir / EXPORT_JSON_FILENAME).write_text(data.export_json)
    except OSError as exc:
        return EnvelopeError(code="artifact_write_failed", message=f"cannot write export json: {exc}")
    return None


def _error_text(envelope: Envelope[HostsIntentRenderData]) -> str:
    return "\n".join(f"error [{err.code}]: {err.message}" for err in envelope.errors)


def _failed(error: EnvelopeError) -> Envelope[HostsIntentRenderData]:
    return Envelope.build(RENDER_HOSTS_INTENT_SCHEMA, HostsIntentRenderData(), [error])
