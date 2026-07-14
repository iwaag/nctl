"""Nautobot client: GraphQL for reads (Phase 1+), a couple of REST probes for `status`."""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel

INTENT_CATALOG_PROBE_PATH = "/api/plugins/intent-catalog/nodes/"


class NautobotError(Exception):
    """Base for all Nautobot client errors."""


class NautobotConnectionError(NautobotError):
    """Nautobot could not be reached (DNS/connect/timeout)."""


class NautobotAuthError(NautobotError):
    """Nautobot rejected the request as unauthenticated/unauthorized."""


class NautobotGraphQLError(NautobotError):
    def __init__(self, errors: list[dict[str, Any]]) -> None:
        self.errors = errors
        super().__init__(f"GraphQL errors: {errors}")


class NautobotInfo(BaseModel):
    reachable: bool
    url: str
    version: str | None = None
    authenticated: bool = False
    intent_catalog: bool = False


class NautobotClient:
    def __init__(self, url: str, token: str | None, timeout: float = 10.0) -> None:
        self.url = url.rstrip("/")
        headers = {"Authorization": f"Token {token}"} if token else {}
        self._client = httpx.Client(base_url=self.url, headers=headers, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "NautobotClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _get(self, path: str) -> httpx.Response:
        try:
            return self._client.get(path)
        except httpx.RequestError as exc:
            raise NautobotConnectionError(f"cannot reach {self.url}: {exc}") from exc

    def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = self._client.post(
                "/api/graphql/", json={"query": query, "variables": variables or {}}
            )
        except httpx.RequestError as exc:
            raise NautobotConnectionError(f"cannot reach {self.url}: {exc}") from exc
        if response.status_code in (401, 403):
            raise NautobotAuthError(f"authentication failed ({response.status_code})")
        response.raise_for_status()
        body = response.json()
        if body.get("errors"):
            raise NautobotGraphQLError(body["errors"])
        return body["data"]

    def ping(self) -> NautobotInfo:
        """Reachability + auth + version (`/api/status/`) and intent-catalog presence.

        Phase 0-EX1 replaces the intent-catalog REST probe with a GraphQL schema
        introspection check once nintent registers its GraphQL types.
        """
        status_response = self._get("/api/status/")
        if status_response.status_code in (401, 403):
            return NautobotInfo(reachable=True, url=self.url, authenticated=False)
        status_response.raise_for_status()
        body = status_response.json()
        version = body.get("nautobot-version")
        intent_catalog_installed = "nautobot_intent_catalog" in (body.get("nautobot-apps") or {})

        intent_catalog_reachable = False
        if intent_catalog_installed:
            probe = self._get(f"{INTENT_CATALOG_PROBE_PATH}?limit=1")
            intent_catalog_reachable = probe.status_code == 200

        return NautobotInfo(
            reachable=True,
            url=self.url,
            version=version,
            authenticated=True,
            intent_catalog=intent_catalog_installed and intent_catalog_reachable,
        )
