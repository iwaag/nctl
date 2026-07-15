import json

import pytest

import nctl_core.dashboard_render as dashboard_render
from nctl_core.config import Config
from nctl_core.dashboard_render import (
    DRIFT_JSON_FILENAME,
    HTML_FILENAME,
    build_dashboard,
    render_dashboard_text,
)
from nctl_core.drift.engine import TargetStatus
from nctl_core.drift.model import Status, Target
from nctl_core.drift_render import DriftData, DriftSourcesData
from nctl_core.output import Envelope, EnvelopeError


def make_config(tmp_path, dashboard_section: str = "") -> Config:
    (tmp_path / "dumps").mkdir(exist_ok=True)
    config_path = tmp_path / "nctl.toml"
    config_path.write_text(
        f"""
[nautobot]
url = "http://nautobot.test"

[inventory]
dumps_dir = "{tmp_path / 'dumps'}"

[ansible]
playbook_dir = "{tmp_path / 'ansible_agdev'}"
inventory = "inventories/generated/hosts_intent.yml"
{dashboard_section}
"""
    )
    return Config.load(config_path)


def _ok_drift_envelope() -> Envelope[DriftData]:
    data = DriftData(
        generated_at="2026-07-16T12:00:00+00:00",
        summary={"converged": 1},
        severity_summary={"error": 0, "warning": 0, "info": 0},
        targets=[
            TargetStatus(
                target=Target(kind="node", slug="agok", name="agok", id="n1"),
                status=Status.CONVERGED,
                diffs=[],
            )
        ],
        sources=DriftSourcesData(fetched_at="2026-07-16T12:00:00+00:00", observed_dump_count=1),
    )
    return Envelope.build("nctl.drift.v1", data, [])


def _failed_drift_envelope() -> Envelope[DriftData]:
    return Envelope.build(
        "nctl.drift.v1", DriftData(), [EnvelopeError(code="nautobot_fetch_failed", message="boom")]
    )


def test_build_dashboard_writes_artifacts_to_configured_out_dir(tmp_path, monkeypatch):
    out_dir = tmp_path / "dash"
    cfg = make_config(tmp_path, f'[dashboard]\nout_dir = "{out_dir}"\nurl = "http://lan.test/dash/"')
    drift_envelope = _ok_drift_envelope()
    monkeypatch.setattr(dashboard_render, "build_drift", lambda config: drift_envelope)

    envelope = build_dashboard(cfg)

    assert envelope.ok
    html = (out_dir / HTML_FILENAME).read_text()
    assert '"agok"' in html
    assert json.loads((out_dir / DRIFT_JSON_FILENAME).read_text()) == json.loads(drift_envelope.to_json())
    assert envelope.data.html_path == str(out_dir / HTML_FILENAME)
    assert envelope.data.generated_at == "2026-07-16T12:00:00+00:00"
    assert envelope.data.summary == {"converged": 1}
    assert envelope.data.dashboard_url == "http://lan.test/dash/"
    assert envelope.data.status_push.pushed is False
    assert not list(out_dir.glob(".*.tmp"))


def test_build_dashboard_out_dir_argument_overrides_config(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, f'[dashboard]\nout_dir = "{tmp_path / "unused"}"')
    monkeypatch.setattr(dashboard_render, "build_drift", lambda config: _ok_drift_envelope())
    override = tmp_path / "elsewhere"

    envelope = build_dashboard(cfg, out_dir=override)

    assert envelope.ok
    assert (override / HTML_FILENAME).is_file()
    assert not (tmp_path / "unused").exists()


def test_build_dashboard_from_file_skips_drift_computation(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, f'[dashboard]\nout_dir = "{tmp_path / "dash"}"')
    monkeypatch.setattr(
        dashboard_render, "build_drift", lambda config: pytest.fail("build_drift must not be called")
    )
    payload_path = tmp_path / "saved.json"
    payload_path.write_text(_ok_drift_envelope().to_json())

    envelope = build_dashboard(cfg, from_file=payload_path)

    assert envelope.ok
    assert envelope.data.summary == {"converged": 1}
    assert (tmp_path / "dash" / HTML_FILENAME).is_file()


def test_build_dashboard_from_file_rejects_other_schemas(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, f'[dashboard]\nout_dir = "{tmp_path / "dash"}"')
    payload_path = tmp_path / "saved.json"
    payload_path.write_text(json.dumps({"schema": "nctl.status.v1", "data": {}}))

    envelope = build_dashboard(cfg, from_file=payload_path)

    assert not envelope.ok
    assert envelope.errors[0].code == "drift_payload_schema_mismatch"
    assert not (tmp_path / "dash").exists()


def test_build_dashboard_from_file_unreadable(tmp_path):
    cfg = make_config(tmp_path)

    envelope = build_dashboard(cfg, from_file=tmp_path / "missing.json")

    assert not envelope.ok
    assert envelope.errors[0].code == "drift_payload_unreadable"


def test_failed_drift_still_writes_page_and_fails_the_envelope(tmp_path, monkeypatch):
    out_dir = tmp_path / "dash"
    cfg = make_config(tmp_path, f'[dashboard]\nout_dir = "{out_dir}"')
    monkeypatch.setattr(dashboard_render, "build_drift", lambda config: _failed_drift_envelope())

    envelope = build_dashboard(cfg)

    assert not envelope.ok
    assert envelope.errors[0].code == "nautobot_fetch_failed"
    html = (out_dir / HTML_FILENAME).read_text()
    assert "nautobot_fetch_failed" in html
    assert envelope.data.status_push.pushed is False


def test_render_dashboard_text_ok(tmp_path, monkeypatch):
    out_dir = tmp_path / "dash"
    cfg = make_config(tmp_path, f'[dashboard]\nout_dir = "{out_dir}"\nurl = "http://lan.test/dash/"')
    monkeypatch.setattr(dashboard_render, "build_drift", lambda config: _ok_drift_envelope())

    text = render_dashboard_text(build_dashboard(cfg))

    assert f"dashboard: {out_dir / HTML_FILENAME}" in text
    assert "served at: http://lan.test/dash/" in text
    assert "summary: converged=1" in text
    assert "status push: skipped" in text


def test_render_dashboard_text_failure(tmp_path):
    cfg = make_config(tmp_path)

    text = render_dashboard_text(build_dashboard(cfg, from_file=tmp_path / "missing.json"))

    assert "error [drift_payload_unreadable]" in text


def test_dashboard_config_defaults(tmp_path):
    cfg = make_config(tmp_path)

    assert cfg.dashboard.url is None
    assert cfg.dashboard.resolved_out_dir().name == "dashboard"
    assert "~" not in str(cfg.dashboard.resolved_out_dir())
