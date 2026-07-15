import json

import httpx
import respx

import nctl_core.dashboard_render as dashboard_render
from nctl_core.dashboard.push import push_statuses
from nctl_core.dashboard_render import build_dashboard
from nctl_core.drift.engine import TargetStatus
from nctl_core.drift.model import Status, Target
from nctl_core.drift_render import DriftData, DriftSourcesData
from nctl_core.nautobot import NautobotClient
from nctl_core.output import Envelope

from test_dashboard_render import make_config

BASE_URL = "http://nautobot.test"
NODES = f"{BASE_URL}/api/plugins/intent-catalog/nodes"
SERVICES = f"{BASE_URL}/api/plugins/intent-catalog/services"

GENERATED_AT = "2026-07-16T12:00:00+00:00"


def _target(kind: str, status: Status, *, slug=None, name=None, id=None) -> TargetStatus:
    return TargetStatus(target=Target(kind=kind, slug=slug, name=name, id=id), status=status, diffs=[])


def _drift_data(targets: list[TargetStatus]) -> DriftData:
    return DriftData(
        generated_at=GENERATED_AT,
        summary={},
        severity_summary={},
        targets=targets,
        sources=DriftSourcesData(),
    )


def _push(targets: list[TargetStatus]):
    with NautobotClient(BASE_URL, "test-token") as client:
        return push_statuses(client, _drift_data(targets))


@respx.mock
def test_push_patches_nodes_and_services_by_id():
    node_route = respx.patch(f"{NODES}/n1/").mock(return_value=httpx.Response(200, json={}))
    service_route = respx.patch(f"{SERVICES}/s1/").mock(return_value=httpx.Response(200, json={}))

    result = _push(
        [
            _target("node", Status.CONVERGED, slug="agok", id="n1"),
            _target("service", Status.DRIFTING, name="web", id="s1"),
        ]
    )

    assert result.pushed is True
    assert (result.attempted, result.updated, result.skipped_no_row, result.failed) == (2, 2, 0, 0)
    node_body = json.loads(node_route.calls.last.request.content)
    assert node_body == {"reconciliation_status": "converged", "reconciliation_checked_at": GENERATED_AT}
    service_body = json.loads(service_route.calls.last.request.content)
    assert service_body["reconciliation_status"] == "drifting"


@respx.mock
def test_push_skips_open_set_kinds_without_a_route():
    result = _push([_target("production", Status.DRIFTING, name="composition", id="x1")])

    assert (result.attempted, result.updated, result.skipped_no_row, result.failed) == (1, 0, 1, 0)
    assert result.errors == []


@respx.mock
def test_push_counts_404_as_skipped_no_row():
    respx.patch(f"{SERVICES}/s1/").mock(return_value=httpx.Response(404, json={"detail": "Not found."}))

    result = _push([_target("service", Status.UNKNOWN, name="web", id="s1")])

    assert (result.attempted, result.updated, result.skipped_no_row, result.failed) == (1, 0, 1, 0)
    assert result.errors == []


@respx.mock
def test_push_counts_server_errors_as_failed_and_continues():
    respx.patch(f"{NODES}/n1/").mock(return_value=httpx.Response(500, text="boom"))
    respx.patch(f"{NODES}/n2/").mock(return_value=httpx.Response(200, json={}))

    result = _push(
        [
            _target("node", Status.DRIFTING, slug="agbad", id="n1"),
            _target("node", Status.CONVERGED, slug="agok", id="n2"),
        ]
    )

    assert (result.attempted, result.updated, result.skipped_no_row, result.failed) == (2, 1, 0, 1)
    assert "node agbad: HTTP 500" in result.errors[0]


@respx.mock
def test_push_connection_error_degrades_to_failed():
    respx.patch(f"{NODES}/n1/").mock(side_effect=httpx.ConnectError("down"))

    result = _push([_target("node", Status.CONVERGED, slug="agok", id="n1")])

    assert (result.attempted, result.updated, result.skipped_no_row, result.failed) == (1, 0, 0, 1)
    assert "node agok" in result.errors[0]


@respx.mock
def test_push_looks_up_missing_id_by_slug():
    respx.get(f"{NODES}/").mock(
        return_value=httpx.Response(200, json={"count": 1, "results": [{"id": "n9"}]})
    )
    patch_route = respx.patch(f"{NODES}/n9/").mock(return_value=httpx.Response(200, json={}))

    result = _push([_target("node", Status.CONVERGED, slug="agok")])

    assert (result.updated, result.skipped_no_row, result.failed) == (1, 0, 0)
    assert patch_route.called
    request = respx.calls[0].request
    assert request.url.params["slug"] == "agok"


@respx.mock
def test_push_lookup_without_unique_match_is_skipped():
    respx.get(f"{NODES}/").mock(return_value=httpx.Response(200, json={"count": 0, "results": []}))

    result = _push([_target("node", Status.CONVERGED, slug="aggone")])

    assert (result.updated, result.skipped_no_row, result.failed) == (0, 1, 0)


@respx.mock
def test_build_dashboard_pushes_after_write(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, f'[dashboard]\nout_dir = "{tmp_path / "dash"}"')
    drift_envelope = Envelope.build(
        "nctl.drift.v1", _drift_data([_target("node", Status.CONVERGED, slug="agok", id="n1")]), []
    )
    monkeypatch.setattr(dashboard_render, "build_drift", lambda config: drift_envelope)
    respx.patch(f"{NODES}/n1/").mock(return_value=httpx.Response(200, json={}))

    envelope = build_dashboard(cfg)

    assert envelope.ok
    push = envelope.data.status_push
    assert push.pushed is True
    assert (push.attempted, push.updated, push.failed) == (1, 1, 0)


@respx.mock
def test_build_dashboard_push_failure_keeps_envelope_ok(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, f'[dashboard]\nout_dir = "{tmp_path / "dash"}"')
    drift_envelope = Envelope.build(
        "nctl.drift.v1", _drift_data([_target("node", Status.CONVERGED, slug="agok", id="n1")]), []
    )
    monkeypatch.setattr(dashboard_render, "build_drift", lambda config: drift_envelope)
    respx.patch(f"{NODES}/n1/").mock(return_value=httpx.Response(500, text="boom"))

    envelope = build_dashboard(cfg)

    assert envelope.ok
    assert envelope.data.status_push.failed == 1
    assert (tmp_path / "dash" / "index.html").is_file()
