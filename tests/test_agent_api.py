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
        if request.url.path == "/api/session/active":
            return httpx.Response(200, json={"data": {"ses_new": {}}})
        raise AssertionError(request.url)

    with _client(handler) as api:
        assert api.list_sessions("/work") == [{"id": "ses_old", "title": "old"}]
        assert api.create_session()["id"] == "ses_new"
        assert reply_text(api.send_message("ses_new", "/work", "hello")) == "done"
        assert api.is_active("ses_new") is True
        assert api.interrupt("ses_new") is True
    assert requests[0].url.params["directory"] == "/work"
    assert json.loads(requests[2].content) == {"parts": [{"type": "text", "text": "hello"}]}
    assert requests[2].url.params["directory"] == "/work"


def test_send_and_wait_polls_past_tool_steps_for_completed_text_reply():
    calls = 0

    def handler(request):
        nonlocal calls
        if request.method == "POST":
            return httpx.Response(200, json={"info": {"id": "msg_user"}, "parts": []})
        calls += 1
        if calls == 1:
            return httpx.Response(200, json=[{"info": {"id": "msg_old", "role": "assistant", "time": {"completed": 1}}, "parts": [{"type": "text", "text": "old"}]}])
        return httpx.Response(200, json=[
            {"info": {"id": "msg_tool", "role": "assistant", "time": {"completed": 2}}, "parts": [{"type": "tool"}]},
            {"info": {"id": "msg_reply", "role": "assistant", "time": {"completed": 3}}, "parts": [{"type": "text", "text": "finished"}]},
        ])

    with _client(handler) as api:
        assert reply_text(api.send_and_wait("ses_new", "/work", "go", 1)) == "finished"


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


def test_send_and_wait_reports_interrupted_after_active_session_stops():
    active_calls = 0

    def handler(request):
        nonlocal active_calls
        if request.method == "POST":
            return httpx.Response(200, json={"info": {"id": "msg_user"}, "parts": []})
        if request.url.path.endswith("/message"):
            return httpx.Response(200, json=[])
        active_calls += 1
        return httpx.Response(200, json={"data": {"ses_new": {}} if active_calls == 1 else {}})

    with _client(handler) as api:
        with pytest.raises(AgentApiError) as exc:
            api.send_and_wait("ses_new", "/work", "go", 1)
    assert exc.value.code == "agent_interrupted"


def test_send_and_wait_reports_missing_reply_after_inactive_grace():
    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"info": {"id": "msg_user"}, "parts": []})
        if request.url.path.endswith("/message"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"data": {}})

    with _client(handler) as api:
        with pytest.raises(AgentApiError) as exc:
            api.send_and_wait("ses_new", "/work", "go", 1, inactive_grace_seconds=0.01)
    assert exc.value.code == "agent_reply_missing"


def test_interrupt_refusal_is_structured():
    with _client(lambda request: httpx.Response(409)) as api:
        with pytest.raises(AgentApiError) as exc:
            api.interrupt("ses_busy")
    assert exc.value.code == "agent_api_error"
    assert exc.value.detail["status_code"] == 409
