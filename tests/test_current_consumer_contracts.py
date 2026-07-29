"""Exact schemas required by the current named nctl consumers."""

from __future__ import annotations

from nctl_core.braindump import (
    BraindumpCreateData,
    BraindumpListData,
    BraindumpReviewData,
    BraindumpReviewDeleteData,
    BraindumpShowData,
)
from nctl_core.dnsmasq_apply import DnsmasqApplyData
from nctl_core.dnsmasq_render import DnsmasqRenderData
from nctl_core.drift_render import DriftData
from nctl_core.events import EventRecord
from nctl_core.hosts_intent_render import HostsIntentRenderData
from nctl_core.ops_render import OpsListData, OpsShowData
from nctl_core.output import Envelope, EnvelopeError
from nctl_core.production_render import ProductionRenderData
from nctl_core.reconcile.results import ReconcileData
from nctl_core.status import StatusData

# Shared envelope and event records are consumed by `nctl ops` and CLI callers.
CURRENT_EVENT_RECORD_FIELDS = {"ts", "operation_id", "op", "seq", "event", "level", "message", "data"}
CURRENT_ENVELOPE_FIELDS = {"schema_name", "generated_at", "ok", "data", "errors"}
CURRENT_ENVELOPE_ERROR_FIELDS = {"code", "message", "detail"}

# Current command schemas. A coordinated future change updates its writer, reader, documentation,
# and this exact contract in one matched-version rollout.
CURRENT_DATA_FIELDS = {
    "nctl.status.v1": (StatusData, {"operation_id", "nautobot", "dumps", "submodules"}),
    "nctl.drift.v1": (DriftData, {"generated_at", "summary", "severity_summary", "targets", "sources"}),
    "nctl.apply.dnsmasq.v2": (
        DnsmasqApplyData,
        {
            "operation_id",
            "event_log_path",
            "mode",
            "artifact_path",
            "inventory_path",
            "target_group",
            "target_hosts",
            "render_summary",
            "content_sha256",
            "ansible",
            "ssh_preflight",
            "setup",
        },
    ),
    "nctl.render.dnsmasq.v3": (
        DnsmasqRenderData,
        {
            "schema_version",
            "summary",
            "dns_records",
            "dhcp_reservations",
            "dhcp_ranges",
            "skipped",
            "conf",
            "content_sha256",
            "partial_conf_preview",
            "blocked",
            "blocking_findings",
        },
    ),
    "nctl.render.production.v1": (
        ProductionRenderData,
        {"inventory", "report", "inventory_yaml", "report_json"},
    ),
    "nctl.render.hosts_intent.v1": (
        HostsIntentRenderData,
        {"schema_version", "summary", "inventory", "hosts", "skipped", "inventory_yaml", "export_json"},
    ),
    "nctl.ops.list.v1": (OpsListData, {"log_dir", "operations"}),
    "nctl.ops.show.v1": (OpsShowData, {"log_dir", "operation", "events"}),
    "nctl.braindump.list.v1": (BraindumpListData, {"items", "count"}),
    "nctl.braindump.show.v1": (BraindumpShowData, {"braindump"}),
    "nctl.braindump.create.v1": (BraindumpCreateData, {"braindump", "changed"}),
    "nctl.braindump.review.v1": (BraindumpReviewData, {"braindump", "action"}),
    "nctl.braindump.review_delete.v1": (
        BraindumpReviewDeleteData,
        {"braindump", "deleted", "review_id"},
    ),
}


# Reconcile artifacts are also read from disk by `nctl ops show`; a dashboard presentation field
# has no current consumer and must not be reintroduced.
CURRENT_RECONCILE_DATA_FIELDS = {
    "operation_id",
    "mode",
    "scope",
    "state",
    "event_log_path",
    "artifact_dir",
    "plan_path",
    "initial_drift_path",
    "final_drift_path",
    "rounds",
    "manual_review",
    "unsupported",
    "summary",
    "scope_summary",
    "progress_made",
    "ssh_preflight",
}


def test_reconcile_data_fields_are_exact_current_contract_without_dashboard_presentation():
    assert set(ReconcileData.model_fields) == CURRENT_RECONCILE_DATA_FIELDS


def test_event_record_fields_are_the_exact_current_ops_contract():
    assert set(EventRecord.model_fields) == CURRENT_EVENT_RECORD_FIELDS


def test_envelope_wrapper_fields_are_the_exact_current_cli_contract():
    assert set(Envelope.model_fields) == CURRENT_ENVELOPE_FIELDS
    assert set(EnvelopeError.model_fields) == CURRENT_ENVELOPE_ERROR_FIELDS


def test_envelope_data_payloads_are_exact_current_consumer_contracts():
    for schema, (model, current_fields) in CURRENT_DATA_FIELDS.items():
        assert set(model.model_fields) == current_fields, schema
