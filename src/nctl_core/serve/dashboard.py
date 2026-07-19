"""Reference live dashboard page (Phase 5 Step 5): a static HTML page served at `GET /`
that fetches the latest drift/operations snapshots over the documented REST API and
subscribes to `/api/v1/ws` for live updates. Unlike the Phase 3 static dashboard, no data
is embedded server-side -- the page is identical on every request and holds no token; the
token is entered client-side and kept only in `sessionStorage`, per Decision 8.
"""

from __future__ import annotations

from importlib import resources


def render_live_dashboard_html() -> str:
    return resources.files("nctl_core.serve").joinpath("live_dashboard.html").read_text()
