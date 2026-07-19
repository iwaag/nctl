"""Tests for the reference live dashboard page (Phase 5 Step 5, `GET /`)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from nctl_core.config import Config
from nctl_core.serve.app import create_app
from nctl_core.serve.dashboard import render_live_dashboard_html


def _config(tmp_path: Path, **overrides) -> Config:
    base = {
        "nautobot": {"url": "http://nautobot.test"},
        "inventory": {"dumps_dir": tmp_path / "dumps"},
        "events": {"log_dir": tmp_path / "events"},
        "ansible": {"playbook_dir": tmp_path / "ansible", "inventory": "inventory.yml"},
        "dashboard": {"out_dir": tmp_path / "dashboard"},
        "serve": {"auth": "token"},
        "source_path": tmp_path / "nctl.toml",
    }
    base.update(overrides)
    return Config.model_validate(base)


def _request(app, method, path, **kwargs):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(run())


def test_dashboard_route_serves_html_without_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("NCTL_SERVE_TOKEN", "test-serve-token")
    app = create_app(_config(tmp_path))

    response = _request(app, "GET", "/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.text == render_live_dashboard_html()


def test_dashboard_page_embeds_no_token_or_data(tmp_path, monkeypatch):
    monkeypatch.setenv("NCTL_SERVE_TOKEN", "super-secret-token")
    app = create_app(_config(tmp_path))

    response = _request(app, "GET", "/")

    assert "super-secret-token" not in response.text
    # The page fetches drift/operations client-side; nothing is pre-rendered server-side.
    assert "nctl.drift.v1" not in response.text
    assert "nctl.ops.list.v1" not in response.text


def test_dashboard_page_only_talks_to_the_documented_api_surface(tmp_path, monkeypatch):
    monkeypatch.setenv("NCTL_SERVE_TOKEN", "test-serve-token")
    app = create_app(_config(tmp_path))

    html = _request(app, "GET", "/").text

    assert "/api/v1/drift" in html
    assert "/api/v1/operations" in html
    assert "/api/v1/ws" in html


def test_render_live_dashboard_html_is_static_across_calls():
    assert render_live_dashboard_html() == render_live_dashboard_html()
