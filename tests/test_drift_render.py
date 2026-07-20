import json

import httpx
import respx

from nctl_core.config import Config
from nctl_core.drift.model import DiffRecord, Severity, Target
from nctl_core.drift_render import _intent_effect_summary_lines, build_drift, render_drift_text
from nctl_core.nautobot import NautobotConnectionError

BASE_URL = "http://nautobot.test"

EMPTY_DESIRED_RESPONSE = {
    "data": {
        "desired_nodes": [],
        "desired_endpoints": [],
        "desired_ip_ranges": [],
        "desired_node_operational_overrides": [],
        "desired_service_placements": [],
        "desired_services": [],
        "desired_dependencies": [],
    }
}

EMPTY_ACTUAL_RESPONSE = {
    "data": {
        "devices": [],
        "virtual_machines": [],
        "interfaces": [],
        "ip_addresses": [],
    }
}

TWO_NODE_DESIRED_RESPONSE = {
    "data": {
        "desired_nodes": [
            {
                "id": "node-1",
                "slug": "agok",
                "name": "agok",
                "lifecycle": "ACTIVE",
                "node_type": "DEVICE",
                "role": None,
                "accepted_actual_types": ["DEVICE"],
                "expected_spec": {},
                "realized_device": {"id": "dev-1"},
                "realized_vm": None,
            },
            {
                "id": "node-2",
                "slug": "agmissing",
                "name": "agmissing",
                "lifecycle": "ACTIVE",
                "node_type": "DEVICE",
                "role": None,
                "accepted_actual_types": ["DEVICE"],
                "expected_spec": {},
                "realized_device": {"id": "dev-gone"},
                "realized_vm": None,
            },
        ],
        "desired_endpoints": [],
        "desired_ip_ranges": [],
        "desired_node_operational_overrides": [],
        "desired_service_placements": [],
        "desired_services": [
            {
                "id": "svc-1",
                "slug": "web",
                "name": "web",
                "display_name": "Web",
                "service_type": "CONTAINER",
                "lifecycle": "ACTIVE",
                "catalog_namespace": "ns",
                "catalog_metadata_name": "web",
                "requirements": {},
            }
        ],
        "desired_dependencies": [],
    }
}

ONE_DEVICE_ACTUAL_RESPONSE = {
    "data": {
        "devices": [{"id": "dev-1", "name": "agok.local", "serial": None, "platform": None, "_custom_field_data": {}}],
        "virtual_machines": [],
        "interfaces": [],
        "ip_addresses": [],
    }
}


def make_config(tmp_path) -> Config:
    (tmp_path / "dumps").mkdir()
    # A valid (if empty) deployment_profiles.yml by default so tests unrelated to profile
    # loading don't trip Phase 4's new deployment_profiles_unavailable global blocker; tests
    # that specifically exercise missing/invalid profiles build their own tmp_path layout.
    vars_dir = tmp_path / "ansible_agdev" / "vars"
    vars_dir.mkdir(parents=True)
    (vars_dir / "deployment_profiles.yml").write_text("deployment_profiles: {}\n")
    config_path = tmp_path / "nctl.toml"
    config_path.write_text(
        f"""
[nautobot]
url = "{BASE_URL}"

[inventory]
dumps_dir = "{tmp_path / 'dumps'}"

[ansible]
playbook_dir = "{tmp_path / 'ansible_agdev'}"
inventory = "inventories/generated/hosts_intent.yml"
"""
    )
    return Config.load(config_path)


def _mock_graphql(desired_response, actual_response=EMPTY_ACTUAL_RESPONSE):
    respx.post(f"{BASE_URL}/api/graphql/").mock(
        side_effect=[
            httpx.Response(200, json=desired_response),
            httpx.Response(200, json=actual_response),
        ]
    )


@respx.mock
def test_build_drift_ok_with_no_desired_state(tmp_path):
    _mock_graphql(EMPTY_DESIRED_RESPONSE)
    cfg = make_config(tmp_path)

    envelope = build_drift(cfg)

    assert envelope.ok is True
    assert envelope.schema_name == "nctl.drift.v1"
    assert envelope.data.targets == []
    assert envelope.data.summary == {}
    assert envelope.data.severity_summary == {"error": 0, "warning": 0, "info": 0}


@respx.mock
def test_build_drift_reports_per_node_and_service_status(tmp_path):
    _mock_graphql(TWO_NODE_DESIRED_RESPONSE, ONE_DEVICE_ACTUAL_RESPONSE)
    cfg = make_config(tmp_path)

    envelope = build_drift(cfg)

    assert envelope.ok is True
    kinds_and_status = {(t.target.slug or t.target.name): t.status.value for t in envelope.data.targets}
    assert kinds_and_status["agok"] == "converged"
    assert kinds_and_status["agmissing"] == "unknown"
    # A service with no active placement is a visible manual-review warning,
    # not an invented observation error.
    assert kinds_and_status["web"] == "converged"
    assert envelope.data.summary == {"converged": 2, "unknown": 1}


@respx.mock
def test_build_drift_host_filter_scopes_targets_and_summary(tmp_path):
    _mock_graphql(TWO_NODE_DESIRED_RESPONSE, ONE_DEVICE_ACTUAL_RESPONSE)
    cfg = make_config(tmp_path)

    envelope = build_drift(cfg, host="agmissing")

    assert [t.target.slug for t in envelope.data.targets] == ["agmissing"]
    assert envelope.data.summary == {"unknown": 1}


@respx.mock
def test_build_drift_service_filter_scopes_targets(tmp_path):
    _mock_graphql(TWO_NODE_DESIRED_RESPONSE, ONE_DEVICE_ACTUAL_RESPONSE)
    cfg = make_config(tmp_path)

    envelope = build_drift(cfg, service="web")

    assert [t.target.name for t in envelope.data.targets] == ["web"]
    assert envelope.data.summary == {"converged": 1}


@respx.mock
def test_build_drift_reports_source_metadata(tmp_path):
    _mock_graphql(EMPTY_DESIRED_RESPONSE)
    cfg = make_config(tmp_path)

    envelope = build_drift(cfg)

    assert envelope.data.sources.fetched_at
    assert envelope.data.sources.observed_dump_count == 0
    assert envelope.data.sources.observed_errors == []


def test_build_drift_degrades_on_nautobot_failure(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)

    class FailingClient:
        def __init__(self, *a, **kw):
            pass

        def graphql(self, *a, **kw):
            raise NautobotConnectionError("connection refused")

        def close(self):
            pass

    monkeypatch.setattr("nctl_core.drift_render.NautobotClient", FailingClient)

    envelope = build_drift(cfg)

    assert envelope.ok is False
    assert any(err.code == "nautobot_fetch_failed" for err in envelope.errors)


@respx.mock
def test_build_drift_reports_missing_deployment_profiles_as_global_error_without_failing(tmp_path):
    # No vars/deployment_profiles.yml exists under the configured playbook_dir. Phase 4
    # Decision 3: this is a classified global ERROR (deployment_profiles_unavailable), not a
    # silent degrade to `{}` -- but it still doesn't fail the drift command itself.
    _mock_graphql(EMPTY_DESIRED_RESPONSE)
    cfg = make_config(tmp_path)
    (cfg.ansible.resolved_playbook_dir(cfg.source_path.parent) / "vars" / "deployment_profiles.yml").unlink()

    envelope = build_drift(cfg)

    assert envelope.ok is True
    global_targets = [t for t in envelope.data.targets if t.target.kind == "global"]
    codes = [d.code for t in global_targets for d in t.diffs]
    assert "deployment_profiles_unavailable" in codes


def test_render_drift_text_error_lines_when_not_ok(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)

    class FailingClient:
        def __init__(self, *a, **kw):
            pass

        def graphql(self, *a, **kw):
            raise NautobotConnectionError("connection refused")

        def close(self):
            pass

    monkeypatch.setattr("nctl_core.drift_render.NautobotClient", FailingClient)
    envelope = build_drift(cfg)

    text = render_drift_text(envelope)

    assert "error [nautobot_fetch_failed]" in text


@respx.mock
def test_render_drift_text_lists_targets_diffs_and_summary(tmp_path):
    _mock_graphql(TWO_NODE_DESIRED_RESPONSE, ONE_DEVICE_ACTUAL_RESPONSE)
    cfg = make_config(tmp_path)
    envelope = build_drift(cfg)

    text = render_drift_text(envelope)

    assert "agok  converged  1 diff(s)" in text
    assert "[info] intent: lifecycle=active node_type=device" in text
    assert "[info] effective:" in text
    assert "[info] application: state=" in text
    assert "agmissing  unknown  3 diff(s)" in text
    assert "[error] agmissing: missing_actual_node" in text
    assert "[error] agmissing: references realized_device 'dev-gone', which no longer exists in Nautobot" in text
    assert "web  converged  1 diff(s)" in text
    assert "[warning] web: service_has_no_active_placement" in text
    assert "summary: converged=2 unknown=1" in text


@respx.mock
def test_render_drift_text_no_targets_case(tmp_path):
    _mock_graphql(EMPTY_DESIRED_RESPONSE)
    cfg = make_config(tmp_path)
    envelope = build_drift(cfg)

    text = render_drift_text(envelope)

    assert text == "summary: (no targets)"


@respx.mock
def test_envelope_json_round_trips_expected_keys(tmp_path):
    _mock_graphql(EMPTY_DESIRED_RESPONSE)
    cfg = make_config(tmp_path)
    envelope = build_drift(cfg)

    parsed = json.loads(envelope.to_json())

    assert parsed["schema"] == "nctl.drift.v1"
    assert set(parsed["data"].keys()) == {"generated_at", "summary", "severity_summary", "targets", "sources"}


def test_intent_effect_summary_lines_show_config_keys_not_values():
    diff = DiffRecord(
        target=Target(kind="node", slug="agweb", name="agweb", id="node-1"),
        code="intent_effect_summary",
        severity=Severity.INFO,
        message="agweb: recorded intent, effective mechanism, and production application",
        desired={
            "node": {
                "id": "node-1", "slug": "agweb", "name": "agweb", "lifecycle": "active", "node_type": "device",
                "role": None, "accepted_actual_types": ["device"], "accepted_actual_types_source": "derived",
            },
            "endpoints": [],
            "placements": [
                {
                    "id": "p1", "service_id": "svc-1", "service_slug": "web", "instance_name": "primary",
                    "desired_state": "active", "instance_role": None, "deployment_profile": "web",
                    "config_schema_version": "1", "config": {"api_token": "super-secret-value", "enabled": True},
                    "assignment_source": "manual", "endpoint_id": None,
                }
            ],
            "operational_override": None,
        },
        actual={
            "operational_values": {
                "host_os": {
                    "value": "linux", "source": "derived",
                    "source_reference": {"kind": "nodeutils_observation", "observed_system": "Linux", "collected_at": "x"},
                    "override_won": False,
                }
            },
            "operational_finding": None,
            "local_findings": [],
            "production": {
                "state": "included", "reasons": [],
                "placement_effects": [{"placement_id": "p1", "instance_name": "primary", "effect": "applied", "reason": None}],
            },
        },
        sources=["desired", "actual"],
    )

    lines = _intent_effect_summary_lines(diff)
    text = "\n".join(lines)

    assert "config_keys=['api_token', 'enabled']" in text
    assert "super-secret-value" not in text
    assert "host_os=linux (derived)" in text
    assert "state=included" in text
    assert "primary=applied" in text
