from copy import deepcopy
from datetime import datetime, timezone

from nctl_core.drift.service_placement import evaluate_placement_drift, normalize_observed_os

NOW = datetime(2026, 7, 16, 1, tzinfo=timezone.utc)


def placement(**updates):
    row = {
        "placement_id": "p1", "service_id": "s1", "node_id": "n1", "node_slug": "node-a",
        "instance_name": "main", "deployment_profile": "systemd", "realized_device_id": "d1",
        "actual_state_policy": "observed", "expected_host_os": "linux",
    }
    row.update(updates)
    return row


def device(**updates):
    row = {
        "observed_system": "Linux",
        "service_inventory_updated_at": "2026-07-16T00:30:00+00:00",
        "observed_services": {"nomad": {"state": "running", "source": "systemd"}},
    }
    row.update(updates)
    return row


def evaluate(placements=None, devices=None):
    return evaluate_placement_drift(
        [{"id": "s1", "name": "nomad"}], placements if placements is not None else [placement()],
        devices if devices is not None else {"d1": device()}, {"d1": "n1", "d2": "n2"},
        now=NOW, stale_after_hours=24,
    )["s1"]


def gap_codes(report):
    return {gap["code"] for row in report["placements"] for gap in row["gaps"]}


def test_satisfied_running_placement_and_os_match():
    assert evaluate()["status"] == "satisfied"


def test_missing_stopped_stale_and_os_mismatch_are_distinct():
    assert gap_codes(evaluate(devices={"d1": device(observed_services={})})) == {"service_missing"}
    assert gap_codes(evaluate(devices={"d1": device(observed_services={"nomad": {"state": "failed"}})})) == {"service_not_running"}
    assert "service_observation_stale" in gap_codes(evaluate(devices={"d1": device(service_inventory_updated_at="2026-07-14T00:00:00+00:00")}))
    assert "service_placement_os_mismatch" in gap_codes(evaluate(placements=[placement(expected_host_os="macos")]))


def test_missing_device_and_missing_timestamp_are_insufficient_observation():
    assert gap_codes(evaluate(devices={})) == {"service_observation_missing"}
    assert "service_observation_missing" in gap_codes(evaluate(devices={"d1": device(service_inventory_updated_at=None)}))


def test_declared_node_is_observation_exempt():
    report = evaluate(placements=[placement(actual_state_policy="declared")], devices={})
    assert report["status"] == "satisfied"
    assert report["placements"][0]["gaps"] == []


def test_running_service_on_non_target_is_wrong_node_without_changing_membership():
    report = evaluate(devices={"d1": device(), "d2": device()})
    assert report["unexpected_locations"][0]["code"] == "service_observed_on_wrong_node"
    assert len(report["placements"]) == 1


def test_no_active_placement_and_determinism_without_input_mutation():
    placements = [placement()]
    devices = {"d1": device()}
    before = deepcopy((placements, devices))
    assert evaluate(placements=[], devices=devices)["status"] == "no_active_placement"
    assert evaluate(placements=placements, devices=devices) == evaluate(placements=placements, devices=devices)
    assert (placements, devices) == before


def test_observed_os_normalization_matches_nauto_contract():
    assert normalize_observed_os("Linux") == "linux"
    assert normalize_observed_os("Darwin") == "macos"
    assert normalize_observed_os("FreeBSD") is None
