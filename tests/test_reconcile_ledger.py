from __future__ import annotations

import json

import httpx
import pytest
import respx

from nctl_core.artifacts import OperationArtifacts
from nctl_core.drift.model import Target
from nctl_core.jobs import NautobotJobRunner
from nctl_core.nautobot import NautobotClient
from nctl_core.reconcile.ledger import (
    IPAM_SUMMARY_SCHEMA_VERSION,
    LedgerActionError,
    execute_link_actual_node,
    execute_reconcile_ipam,
)
from nctl_core.reconcile.model import ReconcileAction

BASE_URL = "http://nautobot.test"
NODE_ID = "11111111-1111-1111-1111-111111111111"
DEVICE_ID = "22222222-2222-2222-2222-222222222222"
JOB_ID = "33333333-3333-3333-3333-333333333333"
RESULT_ID = "44444444-4444-4444-4444-444444444444"
PROXY_ID = "55555555-5555-5555-5555-555555555555"


def _client() -> NautobotClient:
    return NautobotClient(BASE_URL, "tok")


def _link_action(**overrides) -> ReconcileAction:
    data = dict(
        id="link_actual_node:agweb",
        reconciler_id="link_actual_node",
        action_kind="ledger_patch",
        targets=[Target(kind="node", slug="agweb", name="agweb", id=NODE_ID)],
        claimed_diff_codes=["actual_node_not_linked"],
        reason="test",
        mutates=True,
        requires_observation=False,
        parameters={"candidate": {"object_type": "dcim.device", "id": DEVICE_ID, "name": "agweb"}},
    )
    data.update(overrides)
    return ReconcileAction(**data)


def _ipam_action(**overrides) -> ReconcileAction:
    data = dict(
        id="reconcile_ipam:agweb",
        reconciler_id="reconcile_ipam",
        action_kind="job",
        targets=[Target(kind="node", slug="agweb", name="agweb", id=NODE_ID)],
        claimed_diff_codes=["missing_actual_ip_address"],
        reason="test",
        mutates=True,
        requires_observation=False,
        parameters={"desired_node_slug": "agweb"},
    )
    data.update(overrides)
    return ReconcileAction(**data)


# --- execute_link_actual_node ----------------------------------------------


@respx.mock
def test_link_actual_node_happy_path():
    respx.get(f"{BASE_URL}/api/plugins/intent-catalog/nodes/{NODE_ID}/").mock(
        side_effect=[
            httpx.Response(200, json={"id": NODE_ID, "realized_device": None, "realized_vm": None}),
            httpx.Response(200, json={
                "id": NODE_ID, "realized_device": {"id": DEVICE_ID},
                "realized_device_source": "derived", "realized_vm": None,
            }),
        ]
    )
    patch_route = respx.patch(f"{BASE_URL}/api/plugins/intent-catalog/nodes/{NODE_ID}/").mock(
        return_value=httpx.Response(200, json={"id": NODE_ID, "realized_device": DEVICE_ID})
    )

    result = execute_link_actual_node(_client(), _link_action())

    assert result.field == "realized_device"
    assert result.candidate_id == DEVICE_ID
    assert result.node_slug == "agweb"
    assert json.loads(patch_route.calls[0].request.content) == {
        "realized_device": DEVICE_ID,
        "realized_device_source": "derived",
    }


@respx.mock
def test_link_actual_node_refuses_to_replace_an_existing_link():
    respx.get(f"{BASE_URL}/api/plugins/intent-catalog/nodes/{NODE_ID}/").mock(
        return_value=httpx.Response(200, json={"id": NODE_ID, "realized_device": {"id": "other-device"}})
    )
    # No PATCH route registered: if the code tried to PATCH anyway, respx
    # would raise for the unmocked call, which would also fail this test.

    with pytest.raises(LedgerActionError) as exc:
        execute_link_actual_node(_client(), _link_action())
    assert exc.value.code == "node_already_linked"


@respx.mock
def test_link_actual_node_patch_failure_is_typed():
    respx.get(f"{BASE_URL}/api/plugins/intent-catalog/nodes/{NODE_ID}/").mock(
        return_value=httpx.Response(200, json={"id": NODE_ID, "realized_device": None, "realized_vm": None})
    )
    respx.patch(f"{BASE_URL}/api/plugins/intent-catalog/nodes/{NODE_ID}/").mock(
        return_value=httpx.Response(500, text="boom")
    )

    with pytest.raises(LedgerActionError) as exc:
        execute_link_actual_node(_client(), _link_action())
    assert exc.value.code == "node_link_patch_failed"


@respx.mock
def test_link_actual_node_refetch_mismatch_is_not_confirmed():
    respx.get(f"{BASE_URL}/api/plugins/intent-catalog/nodes/{NODE_ID}/").mock(
        side_effect=[
            httpx.Response(200, json={"id": NODE_ID, "realized_device": None, "realized_vm": None}),
            httpx.Response(200, json={"id": NODE_ID, "realized_device": None, "realized_vm": None}),
        ]
    )
    respx.patch(f"{BASE_URL}/api/plugins/intent-catalog/nodes/{NODE_ID}/").mock(
        return_value=httpx.Response(200, json={"id": NODE_ID})
    )

    with pytest.raises(LedgerActionError) as exc:
        execute_link_actual_node(_client(), _link_action())
    assert exc.value.code == "node_link_not_confirmed"


def test_link_actual_node_rejects_unsupported_candidate_type():
    action = _link_action(parameters={"candidate": {"object_type": "ipam.ipaddress", "id": "x"}})
    with pytest.raises(LedgerActionError) as exc:
        execute_link_actual_node(_client(), action)
    assert exc.value.code == "unsupported_candidate_type"


def test_link_actual_node_rejects_wrong_action():
    action = _link_action(reconciler_id="reconcile_ipam")
    with pytest.raises(LedgerActionError) as exc:
        execute_link_actual_node(_client(), action)
    assert exc.value.code == "wrong_action"


# --- execute_reconcile_ipam -------------------------------------------------


def _runner(tmp_path) -> NautobotJobRunner:
    artifacts = OperationArtifacts.create(tmp_path / "events", "01JTEST")
    return NautobotJobRunner(
        _client(),
        poll_interval_seconds=0.01,
        timeout_seconds=5,
        artifacts=artifacts,
        sleep=lambda _seconds: None,
    )


def _mock_job_run(summary: dict) -> None:
    respx.get(f"{BASE_URL}/api/extras/jobs/").mock(
        return_value=httpx.Response(
            200, json={"results": [{"id": JOB_ID, "name": "Reconcile Desired IPAM Intent"}]}
        )
    )
    respx.post(f"{BASE_URL}/api/extras/jobs/{JOB_ID}/run/").mock(
        return_value=httpx.Response(202, json={"job_result": {"id": RESULT_ID}})
    )
    respx.get(f"{BASE_URL}/api/extras/job-results/{RESULT_ID}/").mock(
        return_value=httpx.Response(200, json={"id": RESULT_ID, "status": {"value": "completed"}})
    )
    respx.get(f"{BASE_URL}/api/extras/file-proxies/").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"id": PROXY_ID, "name": "ipam-reconcile-summary.json", "job_result": {"id": RESULT_ID}}
                ]
            },
        )
    )
    respx.get(f"{BASE_URL}/api/extras/file-proxies/{PROXY_ID}/download/").mock(
        return_value=httpx.Response(200, content=json.dumps(summary).encode("utf-8"))
    )


def _summary(**overrides) -> dict:
    data = {
        "schema_version": IPAM_SUMMARY_SCHEMA_VERSION,
        "scope": {
            "requested_desired_node_slug": "agweb",
            "selected_desired_node_ids": [NODE_ID],
            "selected_desired_node_slugs": ["agweb"],
        },
        "summary": {"endpoints": 1},
        "plans": [
            {
                "action": "noop",
                "desired_endpoint": {"desired_node_slug": "agweb"},
            }
        ],
    }
    data.update(overrides)
    return data


@respx.mock
def test_reconcile_ipam_happy_path(tmp_path):
    _mock_job_run(_summary())

    result = execute_reconcile_ipam(_runner(tmp_path), _ipam_action(), artifact_relative_path="jobs/ipam.json")

    assert result.desired_node_slug == "agweb"
    assert result.conflicts == []
    assert result.skipped == []


@respx.mock
def test_reconcile_ipam_surfaces_conflicts_without_raising(tmp_path):
    _mock_job_run(
        _summary(
            plans=[
                {"action": "conflict", "desired_endpoint": {"desired_node_slug": "agweb"}, "reasons": ["x"]},
                {"action": "skip", "desired_endpoint": {"desired_node_slug": "agweb"}, "reasons": ["y"]},
            ]
        )
    )

    result = execute_reconcile_ipam(_runner(tmp_path), _ipam_action(), artifact_relative_path="jobs/ipam.json")

    assert len(result.conflicts) == 1
    assert len(result.skipped) == 1


@respx.mock
def test_reconcile_ipam_rejects_schema_mismatch(tmp_path):
    _mock_job_run(_summary(schema_version="something.else.v1"))

    with pytest.raises(LedgerActionError) as exc:
        execute_reconcile_ipam(_runner(tmp_path), _ipam_action(), artifact_relative_path="jobs/ipam.json")
    assert exc.value.code == "ipam_summary_schema_mismatch"


@respx.mock
def test_reconcile_ipam_rejects_scope_mismatch(tmp_path):
    _mock_job_run(
        _summary(
            scope={
                "requested_desired_node_slug": "agweb",
                "selected_desired_node_ids": [NODE_ID, "other"],
                "selected_desired_node_slugs": ["agweb", "agdb"],
            }
        )
    )

    with pytest.raises(LedgerActionError) as exc:
        execute_reconcile_ipam(_runner(tmp_path), _ipam_action(), artifact_relative_path="jobs/ipam.json")
    assert exc.value.code == "ipam_summary_scope_mismatch"


@respx.mock
def test_reconcile_ipam_rejects_out_of_scope_plan_rows(tmp_path):
    _mock_job_run(
        _summary(
            plans=[
                {"action": "noop", "desired_endpoint": {"desired_node_slug": "agweb"}},
                {"action": "noop", "desired_endpoint": {"desired_node_slug": "agdb"}},
            ]
        )
    )

    with pytest.raises(LedgerActionError) as exc:
        execute_reconcile_ipam(_runner(tmp_path), _ipam_action(), artifact_relative_path="jobs/ipam.json")
    assert exc.value.code == "ipam_summary_out_of_scope_rows"


@respx.mock
def test_reconcile_ipam_rejects_missing_pinned_endpoint_id(tmp_path):
    _mock_job_run(
        _summary(
            summary={"endpoints": 1},
            plans=[
                {
                    "action": "noop",
                    "desired_endpoint": {"id": "other-endpoint", "desired_node_slug": "agweb"},
                }
            ],
        )
    )
    action = _ipam_action(evidence={"eligible_endpoint_ids": ["e1"]})

    with pytest.raises(LedgerActionError) as exc:
        execute_reconcile_ipam(_runner(tmp_path), action, artifact_relative_path="jobs/ipam.json")
    assert exc.value.code == "ipam_summary_coverage_mismatch"
    assert exc.value.detail["missing_endpoint_ids"] == ["e1"]


@respx.mock
def test_reconcile_ipam_rejects_endpoint_count_mismatch(tmp_path):
    _mock_job_run(
        _summary(
            summary={"endpoints": 2},
            plans=[{"action": "noop", "desired_endpoint": {"id": "e1", "desired_node_slug": "agweb"}}],
        )
    )
    action = _ipam_action(evidence={"eligible_endpoint_ids": ["e1"]})

    with pytest.raises(LedgerActionError) as exc:
        execute_reconcile_ipam(_runner(tmp_path), action, artifact_relative_path="jobs/ipam.json")
    assert exc.value.code == "ipam_summary_coverage_mismatch"


@respx.mock
def test_reconcile_ipam_rejects_zero_endpoint_artifact_when_none_pinned(tmp_path):
    _mock_job_run(_summary(summary={"endpoints": 0}, plans=[]))

    with pytest.raises(LedgerActionError) as exc:
        execute_reconcile_ipam(_runner(tmp_path), _ipam_action(), artifact_relative_path="jobs/ipam.json")
    assert exc.value.code == "ipam_summary_coverage_mismatch"


@respx.mock
def test_reconcile_ipam_separates_applied_from_unresolved_pinned_endpoints(tmp_path):
    _mock_job_run(
        _summary(
            summary={"endpoints": 2},
            plans=[
                {
                    "action": "create_ip_address_applied",
                    "desired_endpoint": {"id": "e1", "desired_node_slug": "agweb"},
                },
                {
                    "action": "conflict",
                    "desired_endpoint": {"id": "e2", "desired_node_slug": "agweb"},
                    "reasons": ["ip_address_type_conflict"],
                },
            ],
        )
    )
    action = _ipam_action(evidence={"eligible_endpoint_ids": ["e1", "e2"]})

    result = execute_reconcile_ipam(_runner(tmp_path), action, artifact_relative_path="jobs/ipam.json")

    assert result.applied_endpoint_ids == ["e1"]
    assert len(result.unresolved_expected_endpoints) == 1
    assert result.unresolved_expected_endpoints[0]["desired_endpoint"]["id"] == "e2"


@respx.mock
def test_reconcile_ipam_unpinned_extra_endpoint_skip_does_not_count_as_unresolved(tmp_path):
    # A node can have other explicit-IP endpoints the planner never pinned
    # (not yet eligible) -- their skip must not fail an otherwise-successful
    # pinned endpoint.
    _mock_job_run(
        _summary(
            summary={"endpoints": 2},
            plans=[
                {"action": "noop", "desired_endpoint": {"id": "e1", "desired_node_slug": "agweb"}},
                {
                    "action": "skip",
                    "desired_endpoint": {"id": "e2", "desired_node_slug": "agweb"},
                    "reasons": ["observation_missing"],
                },
            ],
        )
    )
    action = _ipam_action(evidence={"eligible_endpoint_ids": ["e1"]})

    result = execute_reconcile_ipam(_runner(tmp_path), action, artifact_relative_path="jobs/ipam.json")

    assert result.applied_endpoint_ids == ["e1"]
    assert result.unresolved_expected_endpoints == []
    assert len(result.skipped) == 1  # still surfaced in the full-artifact view


def test_reconcile_ipam_rejects_wrong_action(tmp_path):
    action = _ipam_action(reconciler_id="link_actual_node")
    with pytest.raises(LedgerActionError) as exc:
        execute_reconcile_ipam(_runner(tmp_path), action, artifact_relative_path="jobs/ipam.json")
    assert exc.value.code == "wrong_action"
