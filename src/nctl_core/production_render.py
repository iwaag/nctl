"""`nctl render production`: fetch + compose as one synchronous call (Phase 2 Step 2).

Composition is a pure function of a `SourceSnapshot` plus the ansible_agdev
deployment-profiles map, so the render itself stays fast/synchronous like
`render dnsmasq` — no operation ID or event log; those are reserved for
Phase 4's long-running `apply`/`reconcile`. Writing `--out` adds one bounded
subprocess call (`ansible-inventory --list` against a staged copy) to catch a
malformed inventory before it overwrites the real path; that's still a
render-time safety check, not a long-running operation.
"""

from __future__ import annotations

import subprocess
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from nctl_core.config import Config, ConfigError
from nctl_core.nautobot import NautobotClient, NautobotError
from nctl_core.output import Envelope, EnvelopeError
from nctl_core.production.adapter import build_production_node_inputs
from nctl_core.production.composer import (
    ContractError,
    compose_production_inventory,
    render_production_inventory_yml,
    render_production_report_json,
)
from nctl_core.production.profiles import DeploymentProfilesError, load_deployment_profiles
from nctl_core.sources.snapshot import build_source_snapshot

RENDER_PRODUCTION_SCHEMA = "nctl.render.production.v1"

INVENTORY_FILENAME = "production.yml"
REPORTS_DIRNAME = "production.reports"


class ProductionRenderData(BaseModel):
    inventory: dict[str, Any] = {}
    report: dict[str, Any] = {}
    inventory_yaml: str = ""
    report_json: str = ""


def build_production_render(cfg: Config) -> Envelope[ProductionRenderData]:
    generated_at = datetime.now(timezone.utc).isoformat()
    generation_id = str(uuid.uuid4())
    data = ProductionRenderData()

    try:
        token = cfg.nautobot.resolve_token()
    except ConfigError as exc:
        return _failed(data, EnvelopeError(code="nautobot_token_error", message=str(exc)))

    playbook_dir = cfg.ansible.resolved_playbook_dir(cfg.source_path.parent)
    try:
        profiles, digest = load_deployment_profiles(playbook_dir)
    except DeploymentProfilesError as exc:
        return _failed(data, EnvelopeError(code="deployment_profiles_invalid", message=str(exc)))

    client = NautobotClient(cfg.nautobot.url, token)
    try:
        snapshot = build_source_snapshot(cfg, client)
    except NautobotError as exc:
        return _failed(data, EnvelopeError(code="nautobot_fetch_failed", message=str(exc)))
    finally:
        client.close()

    node_inputs = build_production_node_inputs(snapshot)
    try:
        composition = compose_production_inventory(
            node_inputs,
            profiles,
            generation_id=generation_id,
            generated_at=generated_at,
            deployment_profile_digest=digest,
        )
    except ContractError as exc:
        return _failed(data, EnvelopeError(code=exc.code, message=str(exc)))

    data.inventory = composition.inventory
    data.report = composition.report
    data.inventory_yaml = render_production_inventory_yml(composition)
    data.report_json = render_production_report_json(composition)
    return Envelope.build(RENDER_PRODUCTION_SCHEMA, data, [])


def render_production_inventory_text(envelope: Envelope[ProductionRenderData]) -> str:
    """The inventory YAML itself in the success case; error lines otherwise."""
    if not envelope.ok:
        return _error_text(envelope)
    return envelope.data.inventory_yaml


def render_production_summary_text(envelope: Envelope[ProductionRenderData]) -> str:
    """Human summary for the `--out` case, where the inventory went to a file."""
    if not envelope.ok:
        return _error_text(envelope)
    summary = envelope.data.report.get("summary", {})
    return "\n".join(f"{key}: {value}" for key, value in summary.items())


def default_production_out_dir(cfg: Config) -> Path:
    return cfg.ansible.resolved_inventory(cfg.source_path.parent).parent


def write_production_artifacts(
    envelope: Envelope[ProductionRenderData], out_dir: Path
) -> EnvelopeError | None:
    """Validate with `ansible-inventory --list` against a staged copy, then
    atomically replace `<out_dir>/production.yml` and write the companion
    report under `<out_dir>/production.reports/`."""

    if not envelope.ok:
        return envelope.errors[0] if envelope.errors else EnvelopeError(code="render_failed", message="render failed")

    data = envelope.data
    generation_id = data.report["generation_id"]
    inventory_path = out_dir / INVENTORY_FILENAME
    reports_dir = out_dir / REPORTS_DIRNAME
    report_path = reports_dir / f"{generation_id}.json"

    if shutil.which("ansible-inventory") is None:
        return EnvelopeError(
            code="ansible_executable_missing", message="ansible-inventory must be available on PATH"
        )

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return EnvelopeError(code="artifact_write_failed", message=f"cannot create {out_dir}: {exc}")

    staged_path = out_dir / f".{INVENTORY_FILENAME}.{generation_id}.tmp"
    try:
        staged_path.write_text(data.inventory_yaml)
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
        reports_dir.mkdir(parents=True, exist_ok=True)
        report_path.write_text(data.report_json)
        staged_path.replace(inventory_path)
    except OSError as exc:
        return EnvelopeError(code="artifact_write_failed", message=f"cannot write production artifacts: {exc}")
    return None


def _error_text(envelope: Envelope[ProductionRenderData]) -> str:
    return "\n".join(f"error [{err.code}]: {err.message}" for err in envelope.errors)


def _failed(data: ProductionRenderData, error: EnvelopeError) -> Envelope[ProductionRenderData]:
    return Envelope.build(RENDER_PRODUCTION_SCHEMA, data, [error])
