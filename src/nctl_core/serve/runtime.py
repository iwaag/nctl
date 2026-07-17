"""Serve startup envelope and lazy uvicorn adapter."""

from __future__ import annotations

from pydantic import BaseModel

from nctl_core.config import Config
from nctl_core.output import Envelope

SERVE_SCHEMA = "nctl.serve.v1"


class ServeData(BaseModel):
    host: str
    port: int
    auth: str
    dashboard_url: str


def build_serve_startup(cfg: Config) -> Envelope[ServeData]:
    display_host = "127.0.0.1" if cfg.serve.host in ("0.0.0.0", "::") else cfg.serve.host
    return Envelope.build(
        SERVE_SCHEMA,
        ServeData(
            host=cfg.serve.host,
            port=cfg.serve.port,
            auth=cfg.serve.auth,
            dashboard_url=f"http://{display_host}:{cfg.serve.port}/",
        ),
    )


def render_serve_text(envelope: Envelope[ServeData]) -> str:
    data = envelope.data
    return f"nctl serve listening on {data.host}:{data.port} (auth: {data.auth})\ndashboard: {data.dashboard_url}"


def run_server(cfg: Config) -> None:
    import uvicorn

    from nctl_core.serve.app import create_app

    uvicorn.run(create_app(cfg), host=cfg.serve.host, port=cfg.serve.port)
