import httpx
import pytest
import respx

from nctl_core.nautobot import (
    NautobotAuthError,
    NautobotClient,
    NautobotConnectionError,
    NautobotGraphQLError,
)

BASE_URL = "http://nautobot.test"


@respx.mock
def test_ping_ok_with_intent_catalog_graphql_present():
    respx.get(f"{BASE_URL}/api/status/").mock(
        return_value=httpx.Response(
            200,
            json={"nautobot-version": "3.1.3", "nautobot-apps": {"nautobot_intent_catalog": "0.4.0"}},
        )
    )
    respx.post(f"{BASE_URL}/api/graphql/").mock(
        return_value=httpx.Response(
            200,
            json={"data": {"node": {"name": "DesiredNodeType"}, "endpoint": {"name": "DesiredEndpointType"}}},
        )
    )
    client = NautobotClient(BASE_URL, "tok")
    info = client.ping()
    assert info.reachable is True
    assert info.authenticated is True
    assert info.version == "3.1.3"
    assert info.intent_catalog is True
    assert info.intent_graphql is True


@respx.mock
def test_ping_intent_catalog_installed_but_graphql_types_missing():
    respx.get(f"{BASE_URL}/api/status/").mock(
        return_value=httpx.Response(
            200,
            json={"nautobot-version": "3.1.3", "nautobot-apps": {"nautobot_intent_catalog": "0.3.0"}},
        )
    )
    respx.post(f"{BASE_URL}/api/graphql/").mock(
        return_value=httpx.Response(200, json={"data": {"node": None, "endpoint": None}})
    )
    client = NautobotClient(BASE_URL, "tok")
    info = client.ping()
    assert info.intent_catalog is True
    assert info.intent_graphql is False


@respx.mock
def test_ping_intent_graphql_false_on_graphql_endpoint_error():
    respx.get(f"{BASE_URL}/api/status/").mock(
        return_value=httpx.Response(
            200,
            json={"nautobot-version": "3.1.3", "nautobot-apps": {"nautobot_intent_catalog": "0.4.0"}},
        )
    )
    respx.post(f"{BASE_URL}/api/graphql/").mock(
        return_value=httpx.Response(200, json={"data": None, "errors": [{"message": "boom"}]})
    )
    client = NautobotClient(BASE_URL, "tok")
    info = client.ping()
    assert info.intent_catalog is True
    assert info.intent_graphql is False


@respx.mock
def test_ping_without_intent_catalog_plugin():
    respx.get(f"{BASE_URL}/api/status/").mock(
        return_value=httpx.Response(200, json={"nautobot-version": "3.1.3", "nautobot-apps": {}})
    )
    client = NautobotClient(BASE_URL, "tok")
    info = client.ping()
    assert info.intent_catalog is False
    assert info.intent_graphql is False


@respx.mock
def test_ping_unauthenticated():
    respx.get(f"{BASE_URL}/api/status/").mock(
        return_value=httpx.Response(403, json={"detail": "Authentication credentials were not provided."})
    )
    client = NautobotClient(BASE_URL, None)
    info = client.ping()
    assert info.reachable is True
    assert info.authenticated is False


@respx.mock
def test_ping_connection_refused_raises():
    respx.get(f"{BASE_URL}/api/status/").mock(side_effect=httpx.ConnectError("refused"))
    client = NautobotClient(BASE_URL, "tok")
    with pytest.raises(NautobotConnectionError):
        client.ping()


@respx.mock
def test_graphql_returns_data():
    respx.post(f"{BASE_URL}/api/graphql/").mock(
        return_value=httpx.Response(200, json={"data": {"devices": []}})
    )
    client = NautobotClient(BASE_URL, "tok")
    assert client.graphql("query { devices { id } }") == {"devices": []}


@respx.mock
def test_graphql_raises_on_graphql_errors():
    respx.post(f"{BASE_URL}/api/graphql/").mock(
        return_value=httpx.Response(200, json={"data": None, "errors": [{"message": "bad query"}]})
    )
    client = NautobotClient(BASE_URL, "tok")
    with pytest.raises(NautobotGraphQLError):
        client.graphql("query { nope }")


@respx.mock
def test_graphql_raises_auth_error():
    respx.post(f"{BASE_URL}/api/graphql/").mock(return_value=httpx.Response(401, json={}))
    client = NautobotClient(BASE_URL, "bad-token")
    with pytest.raises(NautobotAuthError):
        client.graphql("query { devices { id } }")


@respx.mock
def test_graphql_connection_error():
    respx.post(f"{BASE_URL}/api/graphql/").mock(side_effect=httpx.ConnectError("refused"))
    client = NautobotClient(BASE_URL, "tok")
    with pytest.raises(NautobotConnectionError):
        client.graphql("query { devices { id } }")


# -- rest_post/rest_patch/rest_delete: consistent 401/403 handling (Phase 2 Step 2.3/2.4) ----------


@respx.mock
def test_rest_post_returns_response_on_success():
    respx.post(f"{BASE_URL}/api/plugins/intent-catalog/braindumps/").mock(
        return_value=httpx.Response(201, json={"id": "bd-1"})
    )
    client = NautobotClient(BASE_URL, "tok")
    response = client.rest_post("/api/plugins/intent-catalog/braindumps/", {"title": "t"})
    assert response.status_code == 201


@respx.mock
def test_rest_post_raises_auth_error_on_401():
    respx.post(f"{BASE_URL}/api/plugins/intent-catalog/braindumps/").mock(return_value=httpx.Response(401))
    client = NautobotClient(BASE_URL, "bad-token")
    with pytest.raises(NautobotAuthError):
        client.rest_post("/api/plugins/intent-catalog/braindumps/", {"title": "t"})


@respx.mock
def test_rest_post_raises_auth_error_on_403():
    respx.post(f"{BASE_URL}/api/plugins/intent-catalog/braindumps/").mock(return_value=httpx.Response(403))
    client = NautobotClient(BASE_URL, "tok")
    with pytest.raises(NautobotAuthError):
        client.rest_post("/api/plugins/intent-catalog/braindumps/", {"title": "t"})


@respx.mock
def test_rest_post_connection_error():
    respx.post(f"{BASE_URL}/api/plugins/intent-catalog/braindumps/").mock(
        side_effect=httpx.ConnectError("refused")
    )
    client = NautobotClient(BASE_URL, "tok")
    with pytest.raises(NautobotConnectionError):
        client.rest_post("/api/plugins/intent-catalog/braindumps/", {"title": "t"})


@respx.mock
def test_rest_patch_returns_response_on_success():
    respx.patch(f"{BASE_URL}/api/plugins/intent-catalog/braindumps/bd-1/").mock(
        return_value=httpx.Response(200, json={"id": "bd-1"})
    )
    client = NautobotClient(BASE_URL, "tok")
    response = client.rest_patch("/api/plugins/intent-catalog/braindumps/bd-1/", {"title": "t"})
    assert response.status_code == 200


@respx.mock
def test_rest_patch_raises_auth_error():
    respx.patch(f"{BASE_URL}/api/plugins/intent-catalog/braindumps/bd-1/").mock(return_value=httpx.Response(403))
    client = NautobotClient(BASE_URL, "tok")
    with pytest.raises(NautobotAuthError):
        client.rest_patch("/api/plugins/intent-catalog/braindumps/bd-1/", {"title": "t"})


@respx.mock
def test_rest_patch_connection_error():
    respx.patch(f"{BASE_URL}/api/plugins/intent-catalog/braindumps/bd-1/").mock(
        side_effect=httpx.ConnectError("refused")
    )
    client = NautobotClient(BASE_URL, "tok")
    with pytest.raises(NautobotConnectionError):
        client.rest_patch("/api/plugins/intent-catalog/braindumps/bd-1/", {"title": "t"})


@respx.mock
def test_rest_delete_returns_response_on_success():
    respx.delete(f"{BASE_URL}/api/plugins/intent-catalog/braindumps/bd-1/").mock(
        return_value=httpx.Response(204)
    )
    client = NautobotClient(BASE_URL, "tok")
    response = client.rest_delete("/api/plugins/intent-catalog/braindumps/bd-1/")
    assert response.status_code == 204


@respx.mock
def test_rest_delete_raises_auth_error_on_401():
    respx.delete(f"{BASE_URL}/api/plugins/intent-catalog/braindumps/bd-1/").mock(return_value=httpx.Response(401))
    client = NautobotClient(BASE_URL, "bad-token")
    with pytest.raises(NautobotAuthError):
        client.rest_delete("/api/plugins/intent-catalog/braindumps/bd-1/")


@respx.mock
def test_rest_delete_raises_auth_error_on_403():
    respx.delete(f"{BASE_URL}/api/plugins/intent-catalog/braindumps/bd-1/").mock(return_value=httpx.Response(403))
    client = NautobotClient(BASE_URL, "tok")
    with pytest.raises(NautobotAuthError):
        client.rest_delete("/api/plugins/intent-catalog/braindumps/bd-1/")


@respx.mock
def test_rest_delete_connection_error():
    respx.delete(f"{BASE_URL}/api/plugins/intent-catalog/braindumps/bd-1/").mock(
        side_effect=httpx.ConnectError("refused")
    )
    client = NautobotClient(BASE_URL, "tok")
    with pytest.raises(NautobotConnectionError):
        client.rest_delete("/api/plugins/intent-catalog/braindumps/bd-1/")


@respx.mock
def test_rest_delete_returns_4xx_response_without_raising_when_not_auth():
    respx.delete(f"{BASE_URL}/api/plugins/intent-catalog/braindumps/bd-1/").mock(
        return_value=httpx.Response(404, json={"detail": "not found"})
    )
    client = NautobotClient(BASE_URL, "tok")
    response = client.rest_delete("/api/plugins/intent-catalog/braindumps/bd-1/")
    assert response.status_code == 404
