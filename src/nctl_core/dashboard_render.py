"""`nctl dashboard`: drift + render + write (+ status push) as one command
(Phase 3 Step 2).

Decision 2: `nctl drift` stays a side-effect-free read; *this* command is the
regeneration entry point — it runs the same `build_drift` internally, renders
the Step 1 page, and atomically replaces `index.html` + `drift.json` in the
configured out dir. Phase 4's `reconcile` will call this code path rather than
shelling out. `--from FILE` renders a previously saved `nctl.drift.v1`
envelope without touching the network.

A failed drift run still writes the artifacts (the page renders the errors —
Step 1's failed-run rendering), and the returned envelope carries the drift
errors, so `ok`/exit code follow the drift run and the file write. The status
push (Step 3) is only attempted for a successful drift payload; its failures
degrade to `status_push` counts, never to `ok: false` (Decision 4).

Synchronous local read+write: no operation ID, no event log (reserved for
Phase 4's long-running `apply`/`reconcile`).
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ValidationError

from nctl_core.config import Config, ConfigError
from nctl_core.dashboard.html import render_dashboard_html
from nctl_core.dashboard.push import StatusPushData, push_statuses
from nctl_core.drift_render import DRIFT_SCHEMA, DriftData, build_drift
from nctl_core.nautobot import NautobotClient
from nctl_core.output import Envelope, EnvelopeError

DASHBOARD_SCHEMA = "nctl.dashboard.v1"
HTML_FILENAME = "index.html"
DRIFT_JSON_FILENAME = "drift.json"


class DashboardData(BaseModel):
    html_path: str = ""
    drift_json_path: str = ""
    generated_at: str = ""
    summary: dict[str, int] = {}
    severity_summary: dict[str, int] = {}
    status_push: StatusPushData = StatusPushData()
    dashboard_url: str | None = None


def build_dashboard(
    cfg: Config, *, out_dir: Path | None = None, from_file: Path | None = None, push: bool = True
) -> Envelope[DashboardData]:
    data = DashboardData(dashboard_url=cfg.dashboard.url)

    if from_file is not None:
        loaded = _load_drift_envelope(from_file)
        if isinstance(loaded, EnvelopeError):
            return Envelope.build(DASHBOARD_SCHEMA, data, [loaded])
        drift_envelope = loaded
    else:
        drift_envelope = build_drift(cfg)

    data.generated_at = drift_envelope.data.generated_at or drift_envelope.generated_at.isoformat()
    data.summary = drift_envelope.data.summary
    data.severity_summary = drift_envelope.data.severity_summary

    resolved_out = out_dir if out_dir is not None else cfg.dashboard.resolved_out_dir()
    write_error = _write_artifacts(resolved_out, render_dashboard_html(drift_envelope), drift_envelope.to_json())
    if write_error is None:
        data.html_path = str(resolved_out / HTML_FILENAME)
        data.drift_json_path = str(resolved_out / DRIFT_JSON_FILENAME)

    if push and drift_envelope.ok and write_error is None:
        data.status_push = _push_statuses(cfg, drift_envelope)

    errors = list(drift_envelope.errors)
    if write_error is not None:
        errors.append(write_error)
    return Envelope.build(DASHBOARD_SCHEMA, data, errors)


def _push_statuses(cfg: Config, drift_envelope: Envelope[DriftData]) -> StatusPushData:
    try:
        token = cfg.nautobot.resolve_token()
    except ConfigError as exc:
        return StatusPushData(errors=[f"nautobot_token_error: {exc}"])

    with NautobotClient(cfg.nautobot.url, token) as client:
        return push_statuses(client, drift_envelope.data)


def _load_drift_envelope(path: Path) -> Envelope[DriftData] | EnvelopeError:
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return EnvelopeError(code="drift_payload_unreadable", message=f"cannot read {path}: {exc}")

    schema = raw.get("schema") if isinstance(raw, dict) else None
    if schema != DRIFT_SCHEMA:
        return EnvelopeError(
            code="drift_payload_schema_mismatch",
            message=f"{path} has schema {schema!r}, expected {DRIFT_SCHEMA!r}",
        )

    try:
        return Envelope[DriftData].model_validate(raw)
    except ValidationError as exc:
        return EnvelopeError(code="drift_payload_invalid", message=f"{path} is not a valid drift envelope: {exc}")


def _write_artifacts(out_dir: Path, html: str, drift_json: str) -> EnvelopeError | None:
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        for name, content in ((HTML_FILENAME, html), (DRIFT_JSON_FILENAME, drift_json)):
            staged = out_dir / f".{name}.tmp"
            staged.write_text(content)
            staged.replace(out_dir / name)
    except OSError as exc:
        return EnvelopeError(code="artifact_write_failed", message=f"cannot write dashboard artifacts: {exc}")
    return None


def render_dashboard_text(envelope: Envelope[DashboardData]) -> str:
    data = envelope.data
    lines: list[str] = []
    if data.html_path:
        lines.append(f"dashboard: {data.html_path}")
        lines.append(f"drift payload: {data.drift_json_path}")
    if data.dashboard_url:
        lines.append(f"served at: {data.dashboard_url}")

    status_line = " ".join(f"{status}={count}" for status, count in sorted(data.summary.items()))
    lines.append(f"summary: {status_line}" if status_line else "summary: (no targets)")

    push = data.status_push
    if push.pushed:
        lines.append(
            f"status push: attempted={push.attempted} updated={push.updated} "
            f"skipped_no_row={push.skipped_no_row} failed={push.failed}"
        )
        lines.extend(f"    push error: {message}" for message in push.errors)
    else:
        lines.append("status push: skipped")

    lines.extend(f"error [{err.code}]: {err.message}" for err in envelope.errors)
    return "\n".join(lines)
