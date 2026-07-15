"""Ported from nintent's `tests/test_production_inventory_contract.py` (Phase 2 Step 2),
restricted to the functions `nctl_core.production.contract` actually ports (see that
module's docstring for what was deliberately dropped: the Job-input byte-contract
transport and the YAML-catalog reference validators, neither used by production
composition). Converted from unittest to pytest; the fixture scenarios are inlined
instead of loaded from nintent's YAML fixture file, to keep this test self-contained.
"""

from __future__ import annotations

import hashlib

import pytest

from nctl_core.production.contract import (
    ACTUAL_MAX_AGE_HOURS,
    ContractError,
    actual_state_problem,
    canonical_json,
    canonical_json_digest,
    evaluate_platform_policy,
    map_placement_config,
    merge_host_variables,
    resolve_connection_variables,
    validate_deployment_profiles,
    validate_endpoint_ownership,
    validate_production_inventory_document,
    validate_production_report,
)

PROFILES = {
    "demo": {
        "group": "demo_server",
        "config_schema_version": "1",
        "variables": {
            "enabled": {"ansible_variable": "demo_enabled", "type": "boolean", "required": False},
            "peers": {"ansible_variable": "demo_peers", "type": "list", "items": "string", "required": False},
        },
    }
}


def assert_contract_error(code: str, function, *args, **kwargs) -> None:
    with pytest.raises(ContractError) as caught:
        function(*args, **kwargs)
    assert caught.value.code == code


def test_canonical_json_bytes_and_digest_are_exact():
    value = {"z": [True, "日本語"], "a": {"n": 3}}
    expected = '{"a":{"n":3},"z":[true,"日本語"]}'

    assert canonical_json(value) == expected
    assert not canonical_json(value).endswith("\n")
    assert canonical_json_digest(value) == hashlib.sha256(expected.encode("utf-8")).hexdigest()


def test_profile_shape_is_closed_and_rejects_duplicate_ansible_variables():
    validate_deployment_profiles(PROFILES)

    bad = {**PROFILES, "demo": {**PROFILES["demo"], "extra": True}}
    assert_contract_error("invalid_contract_keys", validate_deployment_profiles, bad)

    duplicate = {
        **PROFILES,
        "demo": {
            **PROFILES["demo"],
            "variables": {
                **PROFILES["demo"]["variables"],
                "other": {"ansible_variable": "demo_enabled", "type": "boolean", "required": False},
            },
        },
    }
    assert_contract_error("duplicate_variable_assignment", validate_deployment_profiles, duplicate)


def test_placement_config_is_allowlisted_and_typed():
    mapped = map_placement_config("demo", "1", {"enabled": True, "peers": ["a", "b"]}, PROFILES)
    assert mapped == {"demo_enabled": True, "demo_peers": ["a", "b"]}
    assert_contract_error(
        "unknown_config_key", map_placement_config, "demo", "1", {"secret": "must-not-pass"}, PROFILES
    )
    assert_contract_error("unknown_profile", map_placement_config, "missing", "1", {}, PROFILES)
    assert_contract_error(
        "invalid_profile_value_type", map_placement_config, "demo", "1", {"enabled": "true"}, PROFILES
    )


def test_connection_resolution_and_ansible_host():
    local = resolve_connection_variables(
        inventory_hostname="node-a",
        actual_state_policy="required",
        connection_path="local",
        actual_local_ip="192.0.2.10/24",
        local_endpoint={"dns_name": "node-a.example.test", "mdns_name": "node-a.local"},
    )
    assert local["ansible_host"] == "192.0.2.10"
    assert local["local_dns_hostname"] == "node-a.example.test"
    assert "ansible_user" not in local

    tailscale = resolve_connection_variables(
        inventory_hostname="node-a",
        actual_state_policy="required",
        connection_path="tailscale",
        tailscale_endpoint={"ip_address": "100.64.0.10/32"},
    )
    assert tailscale["ansible_host"] == "100.64.0.10"

    assert_contract_error(
        "unresolved_connection_path",
        resolve_connection_variables,
        inventory_hostname="node-a",
        actual_state_policy="required",
        connection_path="tailscale",
    )


def test_freshness_boundary_is_72_hours_inclusive():
    assert ACTUAL_MAX_AGE_HOURS == 72
    assert actual_state_problem("2026-06-24T12:00:00+00:00", "2026-06-27T12:00:00+00:00") is None
    assert actual_state_problem(None, "2026-06-27T12:00:00+00:00") == "missing_actual_data"
    assert (
        actual_state_problem("2026-06-24T11:59:59+00:00", "2026-06-27T12:00:00+00:00") == "stale_actual_data"
    )


def test_evaluate_platform_policy_scenarios():
    host_os, drift = evaluate_platform_policy(
        actual_state_policy="required",
        expected_host_os="linux",
        declared_host_os=None,
        observed_system="Linux",
        power_control="wol",
    )
    assert host_os == "linux"
    assert drift == []

    host_os, drift = evaluate_platform_policy(
        actual_state_policy="declared",
        expected_host_os=None,
        declared_host_os="haos",
        observed_system=None,
        power_control="none",
    )
    assert host_os == "haos"

    host_os, drift = evaluate_platform_policy(
        actual_state_policy="required",
        expected_host_os="linux",
        declared_host_os=None,
        observed_system="Darwin",
        power_control="macos_sleep",
    )
    assert host_os == "macos"
    assert drift[0]["code"] == "desired_actual_os_mismatch"

    assert_contract_error(
        "invalid_platform_power",
        evaluate_platform_policy,
        actual_state_policy="required",
        expected_host_os="linux",
        declared_host_os=None,
        observed_system="Linux",
        power_control="macos_sleep",
    )


def test_endpoint_ownership_mismatch():
    assert_contract_error("endpoint_node_mismatch", validate_endpoint_ownership, "node-a", "node-b")


def test_merge_host_variables_conflict():
    assert_contract_error(
        "conflicting_host_variable",
        merge_host_variables,
        [("demo-primary", {"demo_enabled": True}), ("demo-secondary", {"demo_enabled": False})],
    )


def test_production_inventory_and_report_schema_are_closed():
    generation_id = "12345678-1234-5678-9234-567812345678"
    digest = "a" * 64
    inventory = {
        "all": {
            "vars": {
                "nintent_inventory_schema_version": "1.0",
                "nintent_generation_id": generation_id,
                "nintent_generated_at": "2026-06-27T12:00:00+00:00",
                "nintent_report_path": f"production.reports/{generation_id}.json",
                "nintent_deployment_profile_digest": digest,
            },
            "children": {
                "ssh_hosts": {
                    "hosts": {
                        "node-a": {
                            "host_os": "linux",
                            "connection_path": "local",
                            "nintent_desired_node_id": "node-id",
                            "demo_enabled": True,
                        }
                    }
                },
                "linux": {"hosts": {"node-a": {}}},
                "macos": {"hosts": {}},
                "haos": {"hosts": {}},
                "power_managed": {"hosts": {"node-a": {}}},
                "demo_server": {"hosts": {"node-a": {}}},
            },
        }
    }
    report = {
        "schema_version": "1.0",
        "generation_id": generation_id,
        "generated_at": "2026-06-27T12:00:00+00:00",
        "report_path": f"production.reports/{generation_id}.json",
        "deployment_profile_digest": digest,
        "summary": {
            "eligible": 1,
            "included": 1,
            "skipped": 0,
            "placements": 1,
            "active_placements": 1,
            "inactive_placements": 0,
        },
        "hosts": [],
        "skipped": [],
        "drift": [],
        "errors": [],
    }

    assert validate_production_inventory_document(inventory, PROFILES) is inventory
    assert validate_production_report(report) is report

    inventory["all"]["children"]["ssh_hosts"]["hosts"]["node-a"]["package_manager"] = "apt"
    assert_contract_error(
        "unknown_host_variable", validate_production_inventory_document, inventory, PROFILES
    )

    report["legacy"] = {}
    assert_contract_error("invalid_contract_keys", validate_production_report, report)
