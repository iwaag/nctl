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
def test_ping_ok_with_intent_catalog():
    respx.get(f"{BASE_URL}/api/status/").mock(
        return_value=httpx.Response(
            200,
            json={"nautobot-version": "3.1.3", "nautobot-apps": {"nautobot_intent_catalog": "0.3.0"}},
        )
    )
    respx.get(f"{BASE_URL}/api/plugins/intent-catalog/nodes/?limit=1").mock(
        return_value=httpx.Response(200, json={"count": 0, "results": []})
    )
    client = NautobotClient(BASE_URL, "tok")
    info = client.ping()
    assert info.reachable is True
    assert info.authenticated is True
    assert info.version == "3.1.3"
    assert info.intent_catalog is True


@respx.mock
def test_ping_without_intent_catalog_plugin():
    respx.get(f"{BASE_URL}/api/status/").mock(
        return_value=httpx.Response(200, json={"nautobot-version": "3.1.3", "nautobot-apps": {}})
    )
    client = NautobotClient(BASE_URL, "tok")
    info = client.ping()
    assert info.intent_catalog is False


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
