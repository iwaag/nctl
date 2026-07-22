"""`nctl render dnsmasq`: fetch + render as one synchronous call (Phase 1 Step 3).

No operation ID or event log here: per the roadmap's Phase 0 convention, those
are reserved for long-running operations. Render is a single fast GraphQL round
trip plus a pure computation — `nctl apply dnsmasq` (Step 6) is the long-running
command that gets an operation ID and JSON Lines events.

Phase 2 Step 4: fetches a full `SourceSnapshot` (desired + actual, via
`sources.snapshot.build_source_snapshot`) instead of the old dnsmasq-only
GraphQL query, and derives the DHCP-MAC evaluation inputs from it via
`dnsmasq_query.dnsmasq_inputs_from_snapshot` (the ported `drift/evaluation.py`
logic) instead of reading persisted `intent_evaluations`. This costs one extra
GraphQL round trip (the actual-side query) that the old dnsmasq-only fetch
didn't make; accepted because a single evaluation source library is worth
more than saving one query, and `nctl render dnsmasq`'s output is provably
unchanged (Parity Gate B in `p2/report4.md`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel

from nctl_core.config import Config, ConfigError
from nctl_core.dnsmasq import (
    DnsmasqExport,
    dnsmasq_content_sha256,
    dnsmasq_export_payload,
    export_dnsmasq_records,
    render_dnsmasq_records_conf,
)
from nctl_core.dnsmasq_query import dnsmasq_inputs_from_snapshot
from nctl_core.nautobot import NautobotClient, NautobotError
from nctl_core.output import Envelope, EnvelopeError
from nctl_core.sources.snapshot import SourceSnapshot, build_source_snapshot

RENDER_DNSMASQ_SCHEMA = "nctl.render.dnsmasq.v2"


class DnsmasqRenderData(BaseModel):
    schema_version: str = ""
    summary: dict[str, Any] = {}
    dns_records: list[dict[str, Any]] = []
    dhcp_reservations: list[dict[str, Any]] = []
    dhcp_ranges: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    conf: str = ""
    content_sha256: str = ""


@dataclass(frozen=True)
class DnsmasqRenderResult:
    """The pure, I/O-free result of rendering one `SourceSnapshot` to dnsmasq bytes.

    fix_sshkey3 Step 3: the single implementation `build_dnsmasq_render`
    (CLI/apply) and, from Step 5 on, drift computation both call -- so "what
    would `nctl render dnsmasq` produce right now" can never independently
    drift between the two call sites.
    """

    export: DnsmasqExport
    conf: str
    content_sha256: str


def compute_dnsmasq_render(snapshot: SourceSnapshot) -> DnsmasqRenderResult:
    fetch = dnsmasq_inputs_from_snapshot(snapshot)
    export = export_dnsmasq_records(
        fetch.endpoints,
        ip_ranges=fetch.ip_ranges,
        endpoint_evaluations=fetch.endpoint_evaluations,
        node_evaluations=fetch.node_evaluations,
    )
    conf = render_dnsmasq_records_conf(export)
    return DnsmasqRenderResult(export=export, conf=conf, content_sha256=dnsmasq_content_sha256(conf))


def build_dnsmasq_render(cfg: Config, operation_id: str | None = None) -> Envelope[DnsmasqRenderData]:
    generated_at = datetime.now(timezone.utc).isoformat()

    try:
        token = cfg.nautobot.resolve_token()
    except ConfigError as exc:
        return _failed(EnvelopeError(code="nautobot_token_error", message=str(exc)))

    client = NautobotClient(cfg.nautobot.url, token)
    try:
        snapshot = build_source_snapshot(cfg, client)
    except NautobotError as exc:
        return _failed(EnvelopeError(code="nautobot_fetch_failed", message=str(exc)))
    finally:
        client.close()

    rendered = compute_dnsmasq_render(snapshot)
    payload = dnsmasq_export_payload(rendered.export, generated_at=generated_at, operation_id=operation_id)
    data = DnsmasqRenderData(
        schema_version=payload["schema_version"],
        summary=payload["summary"],
        dns_records=payload["dns_records"],
        dhcp_reservations=payload["dhcp_reservations"],
        dhcp_ranges=payload["dhcp_ranges"],
        skipped=payload["skipped"],
        conf=rendered.conf,
        content_sha256=rendered.content_sha256,
    )
    return Envelope.build(RENDER_DNSMASQ_SCHEMA, data, [])


def render_dnsmasq_conf_text(envelope: Envelope[DnsmasqRenderData]) -> str:
    """The conf itself in the success case; error lines otherwise (pipeable default output)."""
    if not envelope.ok:
        return _error_text(envelope)
    return envelope.data.conf


def render_dnsmasq_summary_text(envelope: Envelope[DnsmasqRenderData]) -> str:
    """Human summary for the `--out` case, where the conf itself went to a file."""
    if not envelope.ok:
        return _error_text(envelope)
    summary = envelope.data.summary
    return "\n".join(
        [
            f"dns_records: {summary.get('dns_records', 0)}",
            f"dhcp_reservations: {summary.get('dhcp_reservations', 0)}",
            f"dhcp_ranges: {summary.get('dhcp_ranges', 0)}",
            f"skipped: {summary.get('skipped', {}).get('details', 0)}",
        ]
    )


def _error_text(envelope: Envelope[DnsmasqRenderData]) -> str:
    return "\n".join(f"error [{err.code}]: {err.message}" for err in envelope.errors)


def _failed(error: EnvelopeError) -> Envelope[DnsmasqRenderData]:
    return Envelope.build(RENDER_DNSMASQ_SCHEMA, DnsmasqRenderData(), [error])
