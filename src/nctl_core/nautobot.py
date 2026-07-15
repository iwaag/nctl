"""Nautobot client: GraphQL for reads (Phase 1+), a couple of REST probes for
`status`, and REST writes (Phase 3+: the 0-EX1 split is reads = GraphQL,
writes = REST)."""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel

INTENT_GRAPHQL_TYPES = ("DesiredNodeType", "DesiredEndpointType")


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
    intent_graphql: bool = False


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

    def rest_get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        try:
            return self._client.get(path, params=params)
        except httpx.RequestError as exc:
            raise NautobotConnectionError(f"cannot reach {self.url}: {exc}") from exc

    def rest_patch(self, path: str, payload: dict[str, Any]) -> httpx.Response:
        try:
            return self._client.patch(path, json=payload)
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

        `intent_catalog` means "app installed" (from `/api/status/`'s `nautobot-apps`).
        `intent_graphql` means "the intent GraphQL types are present in the schema",
        checked via introspection rather than the old intent-catalog REST probe.
        """
        status_response = self._get("/api/status/")
        if status_response.status_code in (401, 403):
            return NautobotInfo(reachable=True, url=self.url, authenticated=False)
        status_response.raise_for_status()
        body = status_response.json()
        version = body.get("nautobot-version")
        intent_catalog_installed = "nautobot_intent_catalog" in (body.get("nautobot-apps") or {})

        intent_graphql = False
        if intent_catalog_installed:
            intent_graphql = self._check_intent_graphql()

        return NautobotInfo(
            reachable=True,
            url=self.url,
            version=version,
            authenticated=True,
            intent_catalog=intent_catalog_installed,
            intent_graphql=intent_graphql,
        )

    def _check_intent_graphql(self) -> bool:
        """Introspect for the intent-catalog GraphQL types nctl consumes in Phases 1-2."""
        fields = " ".join(
            f'{alias}: __type(name: "{type_name}") {{ name }}'
            for alias, type_name in zip(("node", "endpoint"), INTENT_GRAPHQL_TYPES)
        )
        try:
            data = self.graphql(f"{{ {fields} }}")
        except NautobotError:
            return False
        return all(data.get(alias) is not None for alias in ("node", "endpoint"))
