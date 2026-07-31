import json

import httpx
import pytest

from nctl_core.agent_api import AgentApiError, OpenCodeClient, reply_text


def _client(handler):
    return OpenCodeClient("http://agent.test", timeout_seconds=3, client=httpx.Client(transport=httpx.MockTransport(handler), base_url="http://agent.test"))


def test_sessions_create_send_and_interrupt_use_pinned_opencode_paths():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/api/session" and request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "ses_old", "title": "old"}]})
        if request.url.path == "/api/session":
            return httpx.Response(200, json={"data": {"id": "ses_new", "model": {"providerID": "ollama", "modelID": "qwen"}}})
        if request.url.path == "/session/ses_new/message":
            return httpx.Response(200, json={"parts": [{"type": "text", "text": "done"}]})
        if request.url.path == "/api/session/ses_new/interrupt":
            return httpx.Response(204)
        raise AssertionError(request.url)

    with _client(handler) as api:
        assert api.list_sessions("/work") == [{"id": "ses_old", "title": "old"}]
        assert api.create_session()["id"] == "ses_new"
        assert reply_text(api.send_message("ses_new", "/work", "hello")) == "done"
        assert api.interrupt("ses_new") is True
    assert requests[0].url.params["directory"] == "/work"
    assert json.loads(requests[2].content) == {"parts": [{"type": "text", "text": "hello"}]}
    assert requests[2].url.params["directory"] == "/work"


def test_session_not_found_is_structured():
    with _client(lambda request: httpx.Response(404)) as api:
        with pytest.raises(AgentApiError) as exc:
            api.send_message("ses_missing", "/work", "hello")
    assert exc.value.code == "session_not_found"
    assert exc.value.detail == {"session_id": "ses_missing"}


def test_timeout_retains_session_id():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with _client(handler) as api:
        with pytest.raises(AgentApiError) as exc:
            api.send_message("ses_waiting", "/work", "hello")
    assert exc.value.code == "agent_timeout"
    assert exc.value.detail == {"session_id": "ses_waiting"}


def test_interrupt_refusal_is_structured():
    with _client(lambda request: httpx.Response(409)) as api:
        with pytest.raises(AgentApiError) as exc:
            api.interrupt("ses_busy")
    assert exc.value.code == "agent_api_error"
    assert exc.value.detail["status_code"] == 409
