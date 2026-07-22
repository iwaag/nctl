from copy import deepcopy
from datetime import datetime, timezone

from nctl_core.drift.service_placement import ContentSpec, evaluate_placement_drift, normalize_observed_os

NOW = datetime(2026, 7, 16, 1, tzinfo=timezone.utc)


def placement(**updates):
    row = {
        "placement_id": "p1", "service_id": "s1", "node_id": "n1", "node_slug": "node-a",
        "instance_name": "main", "deployment_profile": "systemd", "realized_device_id": "d1",
        "actual_state_policy": "required", "host_os": "linux",
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


def evaluate(placements=None, devices=None, content_spec_by_service_id=None):
    return evaluate_placement_drift(
        [{"id": "s1", "name": "nomad"}], placements if placements is not None else [placement()],
        devices if devices is not None else {"d1": device()}, {"d1": "n1", "d2": "n2"},
        now=NOW, stale_after_hours=24,
        content_spec_by_service_id=content_spec_by_service_id,
    )["s1"]


def gap_codes(report):
    return {gap["code"] for row in report["placements"] for gap in row["gaps"]}


def test_satisfied_running_placement_and_os_match():
    assert evaluate()["status"] == "satisfied"


def test_missing_stopped_and_stale_are_distinct():
    assert gap_codes(evaluate(devices={"d1": device(observed_services={})})) == {"service_missing"}
    assert gap_codes(evaluate(devices={"d1": device(observed_services={"nomad": {"state": "failed"}})})) == {"service_not_running"}
    assert "service_observation_stale" in gap_codes(evaluate(devices={"d1": device(service_inventory_updated_at="2026-07-14T00:00:00+00:00")}))


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


# --- content drift (fix_sshkey3 Step 5) --------------------------------------

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
EXPECTED_PATH = "/etc/dnsmasq.d/nintent-records.conf"
CONTENT_SPEC = {
    "s1": ContentSpec(managed_file_key="records", desired_digest=DIGEST_A, expected_path=EXPECTED_PATH)
}


def _device_with_managed_file(*, status="present", sha256=DIGEST_A, state="running"):
    return device(
        observed_services={
            "nomad": {
                "state": state,
                "source": "systemd",
                "managed_files": {"records": {"path": "/etc/dnsmasq.d/nintent-records.conf", "status": status, "sha256": sha256}},
            }
        }
    )


def test_running_service_with_matching_digest_is_converged():
    report = evaluate(devices={"d1": _device_with_managed_file()}, content_spec_by_service_id=CONTENT_SPEC)
    assert report["status"] == "satisfied"
    assert report["placements"][0]["observed_content_digest"] == DIGEST_A


def test_running_service_with_changed_digest_is_content_mismatch():
    report = evaluate(devices={"d1": _device_with_managed_file(sha256=DIGEST_B)}, content_spec_by_service_id=CONTENT_SPEC)
    assert gap_codes(report) == {"service_config_mismatch"}
    assert report["placements"][0]["observed_content_digest"] == DIGEST_B
    assert report["placements"][0]["desired_content_digest"] == DIGEST_A


def test_process_and_content_dimensions_are_independent():
    # A stopped service with a matching digest is still process drift *and*
    # content-satisfied -- the two dimensions never collapse into one code.
    report = evaluate(devices={"d1": _device_with_managed_file(state="failed")}, content_spec_by_service_id=CONTENT_SPEC)
    assert gap_codes(report) == {"service_not_running"}

    # A running service with a manually-changed file digest is content drift
    # even though the process check alone would report satisfied.
    report = evaluate(devices={"d1": _device_with_managed_file(sha256=DIGEST_B)}, content_spec_by_service_id=CONTENT_SPEC)
    assert gap_codes(report) == {"service_config_mismatch"}


def test_missing_managed_file_observation_is_distinct_from_missing_service():
    # The service is observed as running, but no managed-file result exists
    # yet (e.g. a v1 nodeutils collector, or no observation since rollout).
    report = evaluate(
        devices={"d1": device(observed_services={"nomad": {"state": "running", "source": "systemd"}})},
        content_spec_by_service_id=CONTENT_SPEC,
    )
    assert gap_codes(report) == {"service_config_observation_missing"}


def test_explicit_missing_file_is_service_config_missing():
    report = evaluate(devices={"d1": _device_with_managed_file(status="missing", sha256=None)}, content_spec_by_service_id=CONTENT_SPEC)
    assert gap_codes(report) == {"service_config_missing"}


def test_unreadable_and_too_large_are_service_config_unreadable():
    for status in ("unreadable", "too_large"):
        report = evaluate(devices={"d1": _device_with_managed_file(status=status, sha256=None)}, content_spec_by_service_id=CONTENT_SPEC)
        assert gap_codes(report) == {"service_config_unreadable"}


def test_two_targets_one_converged_one_mismatched():
    placements = [
        placement(placement_id="p1", node_id="n1", node_slug="node-a", realized_device_id="d1"),
        placement(placement_id="p2", node_id="n2", node_slug="node-b", realized_device_id="d2"),
    ]
    devices = {
        "d1": _device_with_managed_file(sha256=DIGEST_A),
        "d2": _device_with_managed_file(sha256=DIGEST_B),
    }
    report = evaluate(placements=placements, devices=devices, content_spec_by_service_id=CONTENT_SPEC)
    by_node = {row["node_slug"]: row for row in report["placements"]}
    assert by_node["node-a"]["gaps"] == []
    assert [g["code"] for g in by_node["node-b"]["gaps"]] == ["service_config_mismatch"]


def test_content_spec_absent_never_produces_content_codes():
    report = evaluate(devices={"d1": _device_with_managed_file(sha256=DIGEST_B)})
    assert gap_codes(report) == set()


# --- observed path is part of content evidence (fix_sshkey4 Step 3) ---------


def _device_with_managed_file_at(path, *, status="present", sha256=DIGEST_A, state="running"):
    return device(
        observed_services={
            "nomad": {
                "state": state,
                "source": "systemd",
                "managed_files": {"records": {"path": path, "status": status, "sha256": sha256}},
            }
        }
    )


def test_stale_observed_path_is_observation_mismatch_even_with_matching_digest():
    # A digest match is not sufficient if the stored observation names a
    # different (e.g. since-rotated) path -- this must be OBSERVATION, never
    # a silent converged/mismatch verdict.
    report = evaluate(
        devices={"d1": _device_with_managed_file_at("/etc/dnsmasq.d/old-records.conf", sha256=DIGEST_A)},
        content_spec_by_service_id=CONTENT_SPEC,
    )
    assert gap_codes(report) == {"service_config_observation_mismatch"}
    assert "service_config_mismatch" not in gap_codes(report)


def test_stale_observed_path_with_missing_status_is_still_observation_mismatch():
    report = evaluate(
        devices={"d1": _device_with_managed_file_at("/etc/dnsmasq.d/old-records.conf", status="missing", sha256=None)},
        content_spec_by_service_id=CONTENT_SPEC,
    )
    assert gap_codes(report) == {"service_config_observation_mismatch"}


def test_matching_path_and_matching_digest_is_converged():
    report = evaluate(
        devices={"d1": _device_with_managed_file_at(EXPECTED_PATH, sha256=DIGEST_A)},
        content_spec_by_service_id=CONTENT_SPEC,
    )
    assert report["status"] == "satisfied"


def test_matching_path_with_different_digest_is_ordinary_content_mismatch():
    report = evaluate(
        devices={"d1": _device_with_managed_file_at(EXPECTED_PATH, sha256=DIGEST_B)},
        content_spec_by_service_id=CONTENT_SPEC,
    )
    assert gap_codes(report) == {"service_config_mismatch"}
