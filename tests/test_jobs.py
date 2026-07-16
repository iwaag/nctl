import json

import httpx
import pytest
import respx

from nctl_core.artifacts import OperationArtifacts
from nctl_core.events import OperationLog
from nctl_core.jobs import (
    NautobotJobError,
    NautobotJobRunner,
    extract_job_result_reference,
    normalize_job_status,
)
from nctl_core.nautobot import NautobotClient, NautobotConnectionError

BASE_URL = "http://nautobot.test"
JOB_ID = "11111111-1111-1111-1111-111111111111"
RESULT_ID = "22222222-2222-2222-2222-222222222222"
PROXY_ID = "33333333-3333-3333-3333-333333333333"


def _runner(tmp_path, *, timeout=30, sleep=lambda _seconds: None, monotonic=lambda: 0.0):
    client = NautobotClient(BASE_URL, "tok")
    artifacts = OperationArtifacts.create(tmp_path / "events", "01JTEST")
    op = OperationLog("reconcile", tmp_path / "events", operation_id="01JTEST")
    return NautobotJobRunner(
        client,
        poll_interval_seconds=0.01,
        timeout_seconds=timeout,
        artifacts=artifacts,
        operation_log=op,
        sleep=sleep,
        monotonic=monotonic,
    )


def _mock_lookup():
    respx.get(f"{BASE_URL}/api/extras/jobs/").mock(
        return_value=httpx.Response(200, json={"results": [{"id": JOB_ID, "name": "Ingest Nodeutils Inventory"}]})
    )


@respx.mock
def test_run_polls_to_success_downloads_exact_artifact_and_emits_sanitized_events(tmp_path):
    _mock_lookup()
    respx.post(f"{BASE_URL}/api/extras/jobs/{JOB_ID}/run/").mock(
        return_value=httpx.Response(
            202,
            json={"job_result": {"id": RESULT_ID, "url": f"/api/extras/job-results/{RESULT_ID}/"}},
        )
    )
    respx.get(f"{BASE_URL}/api/extras/job-results/{RESULT_ID}/").mock(
        side_effect=[
            httpx.Response(200, json={"id": RESULT_ID, "status": {"value": "pending"}}),
            httpx.Response(
                200,
                json={
                    "id": RESULT_ID,
                    "status": {"value": "completed"},
                    "task_kwargs": {"report_batch": "raw-sensitive-report"},
                },
            ),
        ]
    )
    respx.get(f"{BASE_URL}/api/extras/file-proxies/").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"id": "wrong", "name": "other.json", "job_result": {"id": RESULT_ID}},
                    {"id": PROXY_ID, "name": "summary.json", "job_result": {"id": RESULT_ID}},
                ]
            },
        )
    )
    respx.get(f"{BASE_URL}/api/extras/file-proxies/{PROXY_ID}/download/").mock(
        return_value=httpx.Response(200, content=b'{"ingested": 1}\n')
    )

    runner = _runner(tmp_path)
    result = runner.run(
        "Ingest Nodeutils Inventory",
        {"report_batch": "raw-sensitive-report"},
        artifact_name="summary.json",
        artifact_relative_path="jobs/summary.json",
    )

    assert result.status == "completed"
    assert result.poll_count == 2
    assert result.final_result["task_kwargs"] == "<redacted>"
    assert result.result_path.endswith(f"jobs/{RESULT_ID}.json")
    assert json.loads((runner.artifacts.root / "jobs/summary.json").read_text()) == {"ingested": 1}
    assert "raw-sensitive-report" not in (runner.artifacts.root / f"jobs/{RESULT_ID}.json").read_text()
    events = [json.loads(line) for line in (tmp_path / "events/01JTEST.jsonl").read_text().splitlines()]
    assert [event["event"] for event in events] == ["job_started", "job_poll", "job_poll", "job_completed"]
    assert "raw-sensitive-report" not in (tmp_path / "events/01JTEST.jsonl").read_text()


@pytest.mark.parametrize(
    ("body", "location", "expected"),
    [
        ({"job_result": {"id": RESULT_ID}}, None, RESULT_ID),
        ({"result": {"pk": RESULT_ID}}, None, RESULT_ID),
        ({"job_result": f"/api/extras/job-results/{RESULT_ID}/"}, None, RESULT_ID),
        ({}, f"{BASE_URL}/api/extras/job-results/{RESULT_ID}/", RESULT_ID),
    ],
)
def test_extract_job_result_reference_variants(body, location, expected):
    assert extract_job_result_reference(body, location)[0] == expected


@respx.mock
def test_duplicate_exact_job_matches_fail_before_post(tmp_path):
    respx.get(f"{BASE_URL}/api/extras/jobs/").mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"id": "a", "name": "Same"}, {"id": "b", "name": "Same"}]},
        )
    )
    with pytest.raises(NautobotJobError) as exc:
        _runner(tmp_path).run("Same", {})
    assert exc.value.code == "job_lookup_ambiguous"


@respx.mock
def test_job_lookup_auth_failure_is_typed(tmp_path):
    respx.get(f"{BASE_URL}/api/extras/jobs/").mock(
        return_value=httpx.Response(403, json={"detail": "forbidden"})
    )
    with pytest.raises(NautobotJobError) as exc:
        _runner(tmp_path).run("Ingest Nodeutils Inventory", {})
    assert exc.value.code == "job_lookup_failed"
    assert "authentication failed" in str(exc.value)


@respx.mock
def test_job_lookup_connection_failure_propagates_as_connection_error(tmp_path):
    respx.get(f"{BASE_URL}/api/extras/jobs/").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(NautobotConnectionError):
        _runner(tmp_path).run("Ingest Nodeutils Inventory", {})


@respx.mock
def test_terminal_failure_is_not_success(tmp_path):
    _mock_lookup()
    respx.post(f"{BASE_URL}/api/extras/jobs/{JOB_ID}/run/").mock(
        return_value=httpx.Response(202, headers={"Location": f"/api/extras/job-results/{RESULT_ID}/"}, json={})
    )
    respx.get(f"{BASE_URL}/api/extras/job-results/{RESULT_ID}/").mock(
        return_value=httpx.Response(200, json={"status": "failed"})
    )
    with pytest.raises(NautobotJobError) as exc:
        _runner(tmp_path).run("Ingest Nodeutils Inventory", {})
    assert exc.value.code == "job_failed"


@respx.mock
def test_nonterminal_job_times_out(tmp_path):
    _mock_lookup()
    respx.post(f"{BASE_URL}/api/extras/jobs/{JOB_ID}/run/").mock(
        return_value=httpx.Response(202, json={"job_result": {"id": RESULT_ID}})
    )
    respx.get(f"{BASE_URL}/api/extras/job-results/{RESULT_ID}/").mock(
        return_value=httpx.Response(200, json={"status": "running"})
    )
    ticks = iter([0.0, 1.0])
    with pytest.raises(NautobotJobError) as exc:
        _runner(tmp_path, timeout=0.5, monotonic=lambda: next(ticks)).run("Ingest Nodeutils Inventory", {})
    assert exc.value.code == "job_timeout"


@respx.mock
def test_artifact_lookup_rejects_wrong_job_result(tmp_path):
    _mock_lookup()
    respx.post(f"{BASE_URL}/api/extras/jobs/{JOB_ID}/run/").mock(
        return_value=httpx.Response(202, json={"job_result": {"id": RESULT_ID}})
    )
    respx.get(f"{BASE_URL}/api/extras/job-results/{RESULT_ID}/").mock(
        return_value=httpx.Response(200, json={"status": "completed"})
    )
    respx.get(f"{BASE_URL}/api/extras/file-proxies/").mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"id": PROXY_ID, "name": "summary.json", "job_result": {"id": "wrong"}}]},
        )
    )
    with pytest.raises(NautobotJobError) as exc:
        _runner(tmp_path).run(
            "Ingest Nodeutils Inventory",
            {},
            artifact_name="summary.json",
            artifact_relative_path="jobs/summary.json",
        )
    assert exc.value.code == "job_artifact_not_found"


@respx.mock
def test_artifact_lookup_rejects_duplicate_exact_matches(tmp_path):
    _mock_lookup()
    respx.post(f"{BASE_URL}/api/extras/jobs/{JOB_ID}/run/").mock(
        return_value=httpx.Response(202, json={"job_result": {"id": RESULT_ID}})
    )
    respx.get(f"{BASE_URL}/api/extras/job-results/{RESULT_ID}/").mock(
        return_value=httpx.Response(200, json={"status": "completed"})
    )
    row = {"id": PROXY_ID, "name": "summary.json", "job_result": {"id": RESULT_ID}}
    respx.get(f"{BASE_URL}/api/extras/file-proxies/").mock(
        return_value=httpx.Response(200, json={"results": [row, {**row, "id": "duplicate"}]})
    )
    with pytest.raises(NautobotJobError) as exc:
        _runner(tmp_path).run(
            "Ingest Nodeutils Inventory",
            {},
            artifact_name="summary.json",
            artifact_relative_path="jobs/summary.json",
        )
    assert exc.value.code == "job_artifact_ambiguous"


def test_normalize_job_status_accepts_nautobot_choice_shape():
    assert normalize_job_status({"value": "SUCCESS"}) == "success"
