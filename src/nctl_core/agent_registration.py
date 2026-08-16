"""Collect, validate, and ingest observed agag agent registration (agent_intent p1 step 3).

The same collect -> validate -> ingest-through-a-Job shape as `observation.py`:
nctl holds the Zulip and Plane admin credentials and does every read, the
Nautobot Job holds none and only writes what it is handed.

What is read, per `DesiredAgent`:

- **Zulip** `GET /users` for the account behind `zulip_user_id` (accounts are
  keyed on the numeric id, never on email — this realm hides real addresses
  from events), then one `GET /users/{id}/subscriptions/{stream_id}` per
  *desired* channel. A member bot may ask this about any stream it can itself
  see, so no realm-owner credential is needed.
- **Plane** the workspace members list, matched on `plane_user_id`.

`zulip_channels` therefore records the desired channels **confirmed
subscribed**, not the agent's full subscription list — enumerating another
user's subscriptions needs realm-admin rights, and the drift question is only
ever "is the agent on the channels it must hear". A desired channel that does
not exist in the realm reads the same as one the agent is not on: both are a
missing subscription.

This module observes. It never writes to Zulip or Plane, and it never creates a
`DesiredAgent` from what it finds — desired state is not invented from actual
state.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import httpx
from pydantic import BaseModel, Field

from nctl_core.config import Config, ConfigError
from nctl_core.jobs import NautobotJobResult, NautobotJobRunner
from nctl_core.nautobot import NautobotClient, NautobotError
from nctl_core.output import Envelope, EnvelopeError
from nctl_core.sources.desired import DesiredAgent, fetch_desired_snapshot

OBSERVE_SCHEMA = "nctl.observe.agents.v1"
PAYLOAD_SCHEMA = "nctl.agent-registration.v1"
INGEST_JOB_NAME = "Ingest Agent Registration"
INGEST_ARTIFACT_NAME = "agent-registration-ingest-summary.json"
COLLECTOR_NAME = "nctl observe agents"


class AgentRegistrationRow(BaseModel):
    """One agent's observed registration, exactly as it is submitted for ingest."""

    slug: str
    zulip_present: bool = False
    zulip_user_id: int | None = None
    zulip_is_active: bool = False
    zulip_channels: list[str] = Field(default_factory=list)
    plane_present: bool = False
    plane_user_id: str = ""
    plane_role: int | None = None


class AgentRegistrationCollection(BaseModel):
    observed_at: datetime
    collector: str = COLLECTOR_NAME
    agents: list[AgentRegistrationRow] = Field(default_factory=list)

    def as_payload(self) -> dict[str, Any]:
        return {
            "schema_version": PAYLOAD_SCHEMA,
            "observed_at": self.observed_at.isoformat(),
            "collector": self.collector,
            "agents": [row.model_dump() for row in self.agents],
        }


class ObserveAgentsData(BaseModel):
    observed_at: datetime | None = None
    agents: list[AgentRegistrationRow] = Field(default_factory=list)
    job: NautobotJobResult | None = None
    ingested: bool = False


class ZulipReader:
    """Read-only Zulip realm reader over the configured bot credentials."""

    def __init__(self, url: str, email: str, api_key: str, *, verify_tls: bool = True,
                 timeout: float = 15.0) -> None:
        self._client = httpx.Client(
            base_url=url.rstrip("/"), auth=(email, api_key), verify=verify_tls, timeout=timeout
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ZulipReader":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _get(self, path: str) -> dict[str, Any]:
        response = self._client.get(path)
        response.raise_for_status()
        document = response.json()
        if document.get("result") != "success":
            raise httpx.HTTPError(f"zulip {path}: {document.get('msg') or document}")
        return document

    def users(self) -> dict[int, dict[str, Any]]:
        return {row["user_id"]: row for row in self._get("/api/v1/users").get("members", [])}

    def stream_ids(self) -> dict[str, int]:
        return {row["name"]: row["stream_id"] for row in self._get("/api/v1/streams").get("streams", [])}

    def is_subscribed(self, user_id: int, stream_id: int) -> bool:
        return bool(self._get(f"/api/v1/users/{user_id}/subscriptions/{stream_id}").get("is_subscribed"))


class PlaneReader:
    """Read-only Plane CE workspace reader over the configured admin API key."""

    def __init__(self, url: str, workspace_slug: str, api_key: str, timeout: float = 15.0) -> None:
        self.workspace_slug = workspace_slug
        self._client = httpx.Client(
            base_url=url.rstrip("/"), headers={"X-API-Key": api_key}, timeout=timeout
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PlaneReader":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def members(self) -> dict[str, dict[str, Any]]:
        response = self._client.get(f"/api/v1/workspaces/{self.workspace_slug}/members/")
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list):
            raise httpx.HTTPError(f"plane members list is not a list: {type(rows).__name__}")
        return {str(row["id"]): row for row in rows}


def collect_agent_registration(
    agents: list[DesiredAgent],
    zulip: ZulipReader,
    plane: PlaneReader,
    *,
    now: datetime | None = None,
) -> AgentRegistrationCollection:
    """Pure-ish collection: one Zulip/Plane read pass over the desired agents."""
    users = zulip.users()
    streams = zulip.stream_ids()
    members = plane.members()
    rows = []
    for agent in sorted(agents, key=lambda item: item.slug):
        row = AgentRegistrationRow(slug=agent.slug)
        user = users.get(agent.zulip_user_id) if agent.zulip_user_id is not None else None
        if user is not None:
            row.zulip_present = True
            row.zulip_user_id = agent.zulip_user_id
            row.zulip_is_active = bool(user.get("is_active"))
            row.zulip_channels = sorted(
                name
                for name in agent.desired_zulip_channels
                if name in streams and zulip.is_subscribed(agent.zulip_user_id, streams[name])
            )
        member = members.get(agent.plane_user_id) if agent.plane_user_id else None
        if member is not None:
            row.plane_present = True
            row.plane_user_id = agent.plane_user_id
            role = member.get("role")
            row.plane_role = int(role) if isinstance(role, int) else None
        rows.append(row)
    return AgentRegistrationCollection(
        observed_at=now or datetime.now(timezone.utc), agents=rows
    )


def observe_agents(cfg: Config, *, ingest: bool = True) -> Envelope:
    """Collect every DesiredAgent's registration and (by default) ingest it."""
    data = ObserveAgentsData()
    errors: list[EnvelopeError] = []
    try:
        zulip_cfg = cfg.require_zulip()
        plane_cfg = cfg.require_plane()
        config_dir = cfg.source_path.parent
        zulip_email, zulip_key = zulip_cfg.resolve_credentials(config_dir)
        plane_key = plane_cfg.resolve_api_key(config_dir)
    except ConfigError as exc:
        return Envelope.build(
            OBSERVE_SCHEMA, data,
            [EnvelopeError(code="config_error", message=str(exc))],
        )

    client = NautobotClient(cfg.nautobot.url, cfg.nautobot.resolve_token())
    try:
        agents = fetch_desired_snapshot(client).agents
        if not agents:
            return Envelope.build(
                OBSERVE_SCHEMA, data,
                [EnvelopeError(
                    code="no_desired_agents",
                    message="no DesiredAgent rows exist; nothing to observe",
                )],
            )
        with ZulipReader(
            zulip_cfg.url, zulip_email, zulip_key, verify_tls=zulip_cfg.verify_tls
        ) as zulip, PlaneReader(plane_cfg.url, plane_cfg.workspace_slug, plane_key) as plane:
            collection = collect_agent_registration(agents, zulip, plane)
        data.observed_at = collection.observed_at
        data.agents = collection.agents

        if ingest:
            runner = NautobotJobRunner(
                client,
                poll_interval_seconds=cfg.reconcile.job_poll_interval_seconds,
                timeout_seconds=cfg.reconcile.job_timeout_seconds,
            )
            data.job = runner.run(
                INGEST_JOB_NAME,
                {"payload": json.dumps(collection.as_payload(), sort_keys=True)},
                commit=True,
            )
            data.ingested = True
    except (httpx.HTTPError, NautobotError, ValueError) as exc:
        errors.append(EnvelopeError(code="collection_failed", message=f"{type(exc).__name__}: {exc}"))
    finally:
        client.close()

    return Envelope.build(OBSERVE_SCHEMA, data, errors)


def render_observe_agents_text(envelope: Envelope) -> str:
    data = envelope.data
    lines = []
    for row in data.agents:
        channels = ", ".join(row.zulip_channels) or "-"
        lines.append(
            f"{row.slug}: zulip={'yes' if row.zulip_present else 'no'} "
            f"channels=[{channels}] plane={'yes' if row.plane_present else 'no'}"
        )
    if data.ingested:
        lines.append("ingested through the Nautobot Job")
    for error in envelope.errors:
        lines.append(f"error {error.code}: {error.message}")
    return "\n".join(lines) or "no agents observed"
