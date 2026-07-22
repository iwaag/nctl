"""Schema snapshot tests enforcing docs/compatibility.md (Phase 5 Step 6).

Every set pinned here is a floor, not a ceiling: additions are fine (that's why these are
`<=` checks, not `==`), but a rename/removal must fail loudly and point back at the policy
doc instead of silently shipping a breaking change under an unchanged `v1`/`/api/v1` name.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
from fastapi.openapi.utils import get_openapi

from nctl_core.braindump import (
    BraindumpCreateData,
    BraindumpDeleteData,
    BraindumpListData,
    BraindumpReviewData,
    BraindumpReviewDeleteData,
    BraindumpShowData,
    BraindumpUpdateData,
)
from nctl_core.config import Config
from nctl_core.dashboard_render import DashboardData
from nctl_core.dnsmasq_apply import DnsmasqApplyData
from nctl_core.dnsmasq_render import DnsmasqRenderData
from nctl_core.drift_render import DriftData
from nctl_core.events import EventRecord
from nctl_core.hosts_intent_render import HostsIntentRenderData
from nctl_core.ops_render import OpsListData, OpsShowData
from nctl_core.output import Envelope, EnvelopeError
from nctl_core.production_render import ProductionRenderData
from nctl_core.reconcile.executor import ReconcileData
from nctl_core.serve.app import create_app
from nctl_core.serve.runtime import ServeData
from nctl_core.status import StatusData

# 1. EventRecord shape -- docs/compatibility.md section 1.
FROZEN_EVENT_RECORD_FIELDS = {"ts", "operation_id", "op", "seq", "event", "level", "message", "data"}

# 2. Event vocabulary -- docs/compatibility.md section 2.
FROZEN_EVENT_VOCABULARY = {
    "started",
    "step_started",
    "step_completed",
    "warning",
    "failed",
    "finished",
    "rendered",
    "setup_started",
    "setup_completed",
    "setup_dry_run_completed",
    "dry_run_completed",
    "apply_started",
    "apply_completed",
    "plan_created",
    "round_started",
    "action_started",
    "action_completed",
    "actuation_completed",
    "observation_completed",
    "drift_resolved",
    "non_converged",
    "collection_started",
    "reports_retrieved",
}

# 3. Envelope wrapper + EnvelopeError -- docs/compatibility.md section 3.
FROZEN_ENVELOPE_FIELDS = {"schema_name", "generated_at", "ok", "data", "errors"}
FROZEN_ENVELOPE_ERROR_FIELDS = {"code", "message", "detail"}

# 3. `data` payload per schema -- docs/compatibility.md section 3.
FROZEN_DATA_FIELDS = {
    "nctl.status.v1": (StatusData, {"operation_id", "nautobot", "dumps", "submodules"}),
    "nctl.drift.v1": (DriftData, {"generated_at", "summary", "severity_summary", "targets", "sources"}),
    "nctl.dashboard.v1": (
        DashboardData,
        {"html_path", "drift_json_path", "generated_at", "summary", "severity_summary", "status_push", "dashboard_url"},
    ),
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
        },
    ),
    "nctl.render.dnsmasq.v2": (
        DnsmasqRenderData,
        {"schema_version", "summary", "dns_records", "dhcp_reservations", "dhcp_ranges", "skipped", "conf", "content_sha256"},
    ),
    "nctl.render.production.v1": (
        ProductionRenderData,
        {"inventory", "report", "inventory_yaml", "report_json"},
    ),
    "nctl.render.hosts_intent.v1": (
        HostsIntentRenderData,
        {"schema_version", "summary", "inventory", "hosts", "skipped", "inventory_yaml", "export_json"},
    ),
    "nctl.reconcile.v2": (
        ReconcileData,
        {
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
            "dashboard",
            "progress_made",
        },
    ),
    "nctl.ops.list.v1": (OpsListData, {"log_dir", "operations"}),
    "nctl.ops.show.v1": (OpsShowData, {"log_dir", "operation", "events"}),
    "nctl.serve.v1": (ServeData, {"host", "port", "auth", "dashboard_url"}),
    "nctl.braindump.list.v1": (BraindumpListData, {"items", "count"}),
    "nctl.braindump.show.v1": (BraindumpShowData, {"braindump"}),
    "nctl.braindump.create.v1": (BraindumpCreateData, {"braindump", "changed"}),
    "nctl.braindump.update.v1": (BraindumpUpdateData, {"braindump", "changed"}),
    "nctl.braindump.delete.v1": (BraindumpDeleteData, {"id", "title", "deleted", "review_deleted"}),
    "nctl.braindump.review.v1": (BraindumpReviewData, {"braindump", "action"}),
    "nctl.braindump.review_delete.v1": (
        BraindumpReviewDeleteData,
        {"braindump", "deleted", "review_id"},
    ),
}

# 4. HTTP surface under /api/v1 -- docs/compatibility.md section 4 (WS is out-of-band, see below).
FROZEN_API_V1_PATHS = {
    "/api/v1/health",
    "/api/v1/status",
    "/api/v1/drift",
    "/api/v1/operations",
    "/api/v1/operations/{operation_id}",
    "/api/v1/operations/{operation_id}/events",
    "/api/v1/operations/{operation_id}/artifacts",
    "/api/v1/operations/{operation_id}/artifacts/{name}",
}


def test_event_record_fields_are_a_superset_of_the_frozen_set():
    assert FROZEN_EVENT_RECORD_FIELDS <= set(EventRecord.model_fields)


def test_event_vocabulary_names_still_appear_as_emit_literals_in_src():
    # There is no runtime registry of event names (the vocabulary is open-ended per docs/
    # event-log.md), so a rename/removal is caught here by grepping for each frozen name as a
    # quoted `.emit(...)` literal somewhere under src/ -- if a name is renamed in code without
    # updating this list (or vice versa), the missing name shows up below.
    src_root = Path(__file__).resolve().parent.parent / "src" / "nctl_core"
    combined = "\n".join(path.read_text() for path in src_root.rglob("*.py"))
    missing = {name for name in FROZEN_EVENT_VOCABULARY if f'"{name}"' not in combined}
    assert not missing, f"frozen event name(s) no longer found as emit literals: {missing}"


def test_envelope_wrapper_fields_are_a_superset_of_the_frozen_set():
    assert FROZEN_ENVELOPE_FIELDS <= set(Envelope.model_fields)
    assert FROZEN_ENVELOPE_ERROR_FIELDS <= set(EnvelopeError.model_fields)


def test_envelope_data_payloads_are_supersets_of_their_frozen_field_sets():
    for schema, (model, frozen_fields) in FROZEN_DATA_FIELDS.items():
        actual_fields = set(model.model_fields)
        missing = frozen_fields - actual_fields
        assert not missing, f"{schema} ({model.__name__}) dropped frozen field(s): {missing}"


def _config(tmp_path: Path) -> Config:
    return Config.model_validate(
        {
            "nautobot": {"url": "http://nautobot.test"},
            "inventory": {"dumps_dir": tmp_path / "dumps"},
            "events": {"log_dir": tmp_path / "events"},
            "ansible": {"playbook_dir": tmp_path / "ansible", "inventory": "inventory.yml"},
            "dashboard": {"out_dir": tmp_path / "dashboard"},
            "serve": {"auth": "token"},
            "source_path": tmp_path / "nctl.toml",
        }
    )


def test_openapi_paths_are_a_superset_of_the_frozen_v1_surface(tmp_path, monkeypatch):
    monkeypatch.setenv("NCTL_SERVE_TOKEN", "test-serve-token")
    app = create_app(_config(tmp_path))
    document = get_openapi(title=app.title, version=app.version, routes=app.routes)
    paths = set(document["paths"])
    assert FROZEN_API_V1_PATHS <= paths


def test_websocket_route_is_registered_even_though_openapi_omits_it(tmp_path, monkeypatch):
    # FastAPI's get_openapi() does not enumerate WebSocketRoute objects, so /api/v1/ws is
    # pinned here by name against the ASGI route table instead of the OpenAPI document.
    monkeypatch.setenv("NCTL_SERVE_TOKEN", "test-serve-token")
    app = create_app(_config(tmp_path))
    ws_paths = {route.path for route in app.router.routes if getattr(route, "path", None) == "/api/v1/ws"}
    assert ws_paths == {"/api/v1/ws"}


def test_create_operation_post_is_registered_on_operations_path(tmp_path, monkeypatch):
    monkeypatch.setenv("NCTL_SERVE_TOKEN", "test-serve-token")
    app = create_app(_config(tmp_path))
    document = get_openapi(title=app.title, version=app.version, routes=app.routes)
    assert "post" in document["paths"]["/api/v1/operations"]


def test_health_response_shape_is_stable(tmp_path, monkeypatch):
    monkeypatch.setenv("NCTL_SERVE_TOKEN", "test-serve-token")
    app = create_app(_config(tmp_path))

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.get("/api/v1/health")

    response = asyncio.run(run())
    assert response.status_code == 200
    assert {"status", "version"} <= set(response.json())
