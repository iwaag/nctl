"""Small OpenCode 1.18.10 HTTP adapter used through an nctl SSH tunnel."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class AgentApiError(Exception):
    code: str
    message: str
    detail: dict[str, object]


class OpenCodeClient:
    """The intentionally narrow runtime-specific boundary for Phase 5."""

    def __init__(self, base_url: str, *, timeout_seconds: float, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(base_url=base_url, timeout=timeout_seconds)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "OpenCodeClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def list_sessions(self, directory: str) -> list[dict[str, Any]]:
        body = self._request("GET", "/api/session", params={"directory": directory})
        data = body.get("data", body)
        if not isinstance(data, list):
            raise AgentApiError("agent_protocol_error", "OpenCode returned an invalid sessions response", {})
        return [item for item in data if isinstance(item, dict)]

    def create_session(self) -> dict[str, Any]:
        body = self._request("POST", "/api/session", json={})
        data = body.get("data", body)
        if not isinstance(data, dict) or not isinstance(data.get("id"), str):
            raise AgentApiError("agent_protocol_error", "OpenCode returned an invalid created session", {})
        return data

    def send_message(self, session_id: str, directory: str, prompt: str) -> dict[str, Any]:
        return self._request(
            "POST", f"/session/{session_id}/message", params={"directory": directory},
            json={"parts": [{"type": "text", "text": prompt}]}, session_id=session_id,
        )

    def interrupt(self, session_id: str) -> bool:
        self._request("POST", f"/api/session/{session_id}/interrupt", session_id=session_id, accepted={204})
        return True

    def _request(
        self, method: str, path: str, *, session_id: str | None = None, accepted: set[int] | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        accepted = accepted or {200}
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            detail: dict[str, object] = {"session_id": session_id} if session_id else {}
            raise AgentApiError("agent_timeout", "timed out waiting for OpenCode", detail) from exc
        except httpx.HTTPError as exc:
            raise AgentApiError("agent_unreachable", f"OpenCode request failed: {exc}", {}) from exc
        if response.status_code == 404:
            raise AgentApiError("session_not_found", f"OpenCode does not know session {session_id}", {"session_id": session_id or ""})
        if response.status_code not in accepted:
            raise AgentApiError(
                "agent_api_error", f"OpenCode returned HTTP {response.status_code}", {"status_code": response.status_code, "session_id": session_id or ""}
            )
        if response.status_code == 204:
            return {}
        try:
            body = response.json()
        except ValueError as exc:
            raise AgentApiError("agent_protocol_error", "OpenCode returned non-JSON content", {}) from exc
        if not isinstance(body, dict):
            raise AgentApiError("agent_protocol_error", "OpenCode returned an invalid JSON response", {})
        return body


def reply_text(response: dict[str, Any]) -> str:
    """Extract text parts from OpenCode's synchronous message response."""
    parts = response.get("parts", [])
    if not isinstance(parts, list):
        return ""
    return "\n".join(part["text"] for part in parts if isinstance(part, dict) and isinstance(part.get("text"), str))
