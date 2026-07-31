"""Small OpenCode 1.18.10 HTTP adapter used through an nctl SSH tunnel."""

from __future__ import annotations

from dataclasses import dataclass
import time
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
        data = body.get("data", body) if isinstance(body, dict) else body
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

    def get_messages(self, session_id: str, directory: str) -> list[dict[str, Any]]:
        body = self._request("GET", f"/session/{session_id}/message", params={"directory": directory}, session_id=session_id)
        data = body.get("data", body) if isinstance(body, dict) else body
        if not isinstance(data, list):
            raise AgentApiError("agent_protocol_error", "OpenCode returned invalid session messages", {"session_id": session_id})
        return [item for item in data if isinstance(item, dict)]

    def send_and_wait(
        self, session_id: str, directory: str, prompt: str, timeout_seconds: float, *, inactive_grace_seconds: float = 10.0
    ) -> dict[str, Any]:
        """Submit a prompt, then poll its session until a new completed text reply exists."""
        before = self.get_messages(session_id, directory)
        known_ids = {_message_id(item) for item in before}
        self.send_message(session_id, directory, prompt)
        deadline = time.monotonic() + timeout_seconds
        seen_active = False
        inactive_since: float | None = None
        while time.monotonic() < deadline:
            for message in reversed(self.get_messages(session_id, directory)):
                if _message_id(message) in known_ids or not _completed(message) or not reply_text(message):
                    continue
                return message
            active = self.is_active(session_id)
            if seen_active and not active:
                raise AgentApiError("agent_interrupted", "OpenCode stopped the session before a reply completed", {"session_id": session_id})
            if active:
                seen_active = True
                inactive_since = None
            elif inactive_since is None:
                inactive_since = time.monotonic()
            elif time.monotonic() - inactive_since >= inactive_grace_seconds:
                raise AgentApiError("agent_reply_missing", "OpenCode became inactive without a completed text reply", {"session_id": session_id})
            time.sleep(0.25)
        raise AgentApiError("agent_timeout", "timed out waiting for OpenCode reply", {"session_id": session_id})

    def interrupt(self, session_id: str) -> bool:
        self._request("POST", f"/api/session/{session_id}/interrupt", session_id=session_id, accepted={204})
        return True

    def is_active(self, session_id: str) -> bool:
        body = self._request("GET", "/api/session/active")
        data = body.get("data", body) if isinstance(body, dict) else body
        return isinstance(data, dict) and session_id in data

    def _request(
        self, method: str, path: str, *, session_id: str | None = None, accepted: set[int] | None = None, **kwargs: Any
    ) -> Any:
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
        return body


def reply_text(response: dict[str, Any]) -> str:
    """Extract text parts from OpenCode's synchronous message response."""
    parts = response.get("parts", [])
    if not isinstance(parts, list):
        return ""
    return "\n".join(part["text"] for part in parts if isinstance(part, dict) and isinstance(part.get("text"), str))


def _message_id(message: dict[str, Any]) -> str:
    info = message.get("info")
    if isinstance(info, dict) and isinstance(info.get("id"), str):
        return info["id"]
    return str(message.get("id", ""))


def _completed(message: dict[str, Any]) -> bool:
    info = message.get("info")
    if not isinstance(info, dict) or info.get("role") != "assistant":
        return False
    timestamp = info.get("time")
    return isinstance(timestamp, dict) and timestamp.get("completed") is not None
