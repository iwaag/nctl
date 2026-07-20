"""Phase 3 Step 3.1 executable contract for `nctl lifecycle NODE STATE` (plan.md Decision 2).

Frozen before `nctl_core.lifecycle` exists so the contract cannot silently drift from the plan.
Step 3.2 implements the module against exactly this file. `fetch_desired_snapshot` is monkeypatched
(the `dnsmasq_apply.py`/`test_dnsmasq_apply.py` pattern) rather than mocking the full GraphQL
`DESIRED_QUERY` response, since only `DesiredSnapshot.nodes` is relevant here; the REST PATCH is
mocked with `respx` against the real `NautobotClient`.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from nctl_core.nautobot import NautobotClient
from nctl_core.sources.desired import DesiredNode, DesiredSnapshot

BASE_URL = "http://nautobot.test"
NODE_ID = "node-1"
NODE_SLUG = "agpc"


def _node(lifecycle: str) -> DesiredNode:
    return DesiredNode(
        id=NODE_ID,
        slug=NODE_SLUG,
        name="agpc",
        lifecycle=lifecycle,
        node_type="device",
    )


def _snapshot(lifecycle: str) -> DesiredSnapshot:
    return DesiredSnapshot(nodes=[_node(lifecycle)])


def _client() -> NautobotClient:
    return NautobotClient(BASE_URL, "test-token")


def _patch_fetch(monkeypatch, snapshots: list[DesiredSnapshot]):
    calls = iter(snapshots)

    def fake_fetch(client):
        return next(calls)

    monkeypatch.setattr("nctl_core.lifecycle.fetch_desired_snapshot", fake_fetch)


def test_lifecycle_states_are_exactly_the_five_vocabulary_values():
    from nctl_core.lifecycle import LIFECYCLE_STATES

    assert LIFECYCLE_STATES == ("planned", "approved", "active", "deprecated", "retired")


def test_invalid_lifecycle_is_rejected_before_any_fetch_or_patch(monkeypatch):
    from nctl_core.lifecycle import InvalidLifecycleError, set_node_lifecycle

    def fail_fetch(client):
        raise AssertionError("must not fetch for an invalid requested state")

    monkeypatch.setattr("nctl_core.lifecycle.fetch_desired_snapshot", fail_fetch)

    with _client() as client:
        with pytest.raises(InvalidLifecycleError):
            set_node_lifecycle(client, NODE_SLUG, "bogus")


@respx.mock
def test_unknown_node_slug_is_rejected_with_no_patch(monkeypatch):
    from nctl_core.lifecycle import UnknownNodeError, set_node_lifecycle

    _patch_fetch(monkeypatch, [DesiredSnapshot(nodes=[])])
    patch_route = respx.patch(url__regex=r".*/nodes/.*").mock(return_value=httpx.Response(200, json={}))

    with _client() as client:
        with pytest.raises(UnknownNodeError):
            set_node_lifecycle(client, "no-such-node", "active")

    assert patch_route.call_count == 0


@respx.mock
def test_idempotent_no_write_when_current_state_already_matches(monkeypatch):
    from nctl_core.lifecycle import set_node_lifecycle

    _patch_fetch(monkeypatch, [_snapshot("active")])
    patch_route = respx.patch(url__regex=r".*/nodes/.*").mock(return_value=httpx.Response(200, json={}))

    with _client() as client:
        result = set_node_lifecycle(client, NODE_SLUG, "active")

    assert patch_route.call_count == 0
    assert result.changed is False
    assert result.previous_state == "active"
    assert result.requested_state == "active"
    assert result.current_state == "active"
    assert result.node_id == NODE_ID
    assert result.node_slug == NODE_SLUG


@respx.mock
def test_change_patches_exactly_the_lifecycle_field_at_the_expected_path(monkeypatch):
    from nctl_core.lifecycle import set_node_lifecycle

    _patch_fetch(monkeypatch, [_snapshot("planned"), _snapshot("active")])
    patch_route = respx.patch(f"{BASE_URL}/api/plugins/intent-catalog/nodes/{NODE_ID}/").mock(
        return_value=httpx.Response(200, json={})
    )

    with _client() as client:
        result = set_node_lifecycle(client, NODE_SLUG, "active")

    assert patch_route.call_count == 1
    import json

    assert json.loads(patch_route.calls.last.request.content) == {"lifecycle": "active"}
    assert result.changed is True
    assert result.previous_state == "planned"
    assert result.requested_state == "active"
    assert result.current_state == "active"


@respx.mock
def test_rejected_patch_raises_and_does_not_claim_success(monkeypatch):
    from nctl_core.lifecycle import LifecycleUpdateRejectedError, set_node_lifecycle

    _patch_fetch(monkeypatch, [_snapshot("planned")])
    respx.patch(f"{BASE_URL}/api/plugins/intent-catalog/nodes/{NODE_ID}/").mock(
        return_value=httpx.Response(400, json={"lifecycle": ["invalid"]})
    )

    with _client() as client:
        with pytest.raises(LifecycleUpdateRejectedError):
            set_node_lifecycle(client, NODE_SLUG, "active")


@respx.mock
def test_confirmation_mismatch_after_patch_fails_closed(monkeypatch):
    from nctl_core.lifecycle import LifecycleConfirmationMismatchError, set_node_lifecycle

    # Refetch still shows "planned" despite a 200 PATCH response -- must not report changed=True.
    _patch_fetch(monkeypatch, [_snapshot("planned"), _snapshot("planned")])
    respx.patch(f"{BASE_URL}/api/plugins/intent-catalog/nodes/{NODE_ID}/").mock(
        return_value=httpx.Response(200, json={})
    )

    with _client() as client:
        with pytest.raises(LifecycleConfirmationMismatchError):
            set_node_lifecycle(client, NODE_SLUG, "active")
