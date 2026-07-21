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
    map_placement_config,
    merge_host_variables,
    resolve_connection_variables,
    validate_deployment_profiles,
    validate_endpoint_ownership,
    validate_production_inventory_document,
    validate_production_report_v3,
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


def test_endpoint_ownership_mismatch():
    assert_contract_error("endpoint_node_mismatch", validate_endpoint_ownership, "node-a", "node-b")


def test_merge_host_variables_conflict():
    assert_contract_error(
        "conflicting_host_variable",
        merge_host_variables,
        [("demo-primary", {"demo_enabled": True}), ("demo-secondary", {"demo_enabled": False})],
    )


def test_production_inventory_schema_is_closed():
    generation_id = "12345678-1234-5678-9234-567812345678"
    digest = "a" * 64
    inventory = {
        "all": {
            "vars": {
                "nintent_inventory_schema_version": "3.0",
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

    assert validate_production_inventory_document(inventory, PROFILES) is inventory

    old_inventory = {
        **inventory,
        "all": {
            **inventory["all"],
            "vars": {**inventory["all"]["vars"], "nintent_inventory_schema_version": "1.0"},
        },
    }
    assert_contract_error(
        "unsupported_inventory_schema", validate_production_inventory_document, old_inventory, PROFILES
    )

    inventory["all"]["children"]["ssh_hosts"]["hosts"]["node-a"][
        "nintent_operational_config_id"
    ] = "removed"
    assert_contract_error(
        "unknown_host_variable", validate_production_inventory_document, inventory, PROFILES
    )
    del inventory["all"]["children"]["ssh_hosts"]["hosts"]["node-a"][
        "nintent_operational_config_id"
    ]

    inventory["all"]["children"]["ssh_hosts"]["hosts"]["node-a"]["package_manager"] = "apt"
    assert_contract_error(
        "unknown_host_variable", validate_production_inventory_document, inventory, PROFILES
    )


def _value_record(field_name: str) -> dict:
    return {
        "value": None,
        "source": "default",
        "source_reference": {"kind": "safe_default", "field": field_name},
        "override_won": False,
    }


def _v3_node_record(*, state: str, effect: str = "applied", reason: str | None = None) -> dict:
    return {
        "desired": {
            "node": {
                "id": "node-id",
                "slug": "node-a",
                "name": "node-a",
                "lifecycle": "active",
                "node_type": "device",
                "role": None,
                "accepted_actual_types": ["device"],
                "accepted_actual_types_source": "derived",
            },
            "endpoints": [],
            "placements": [
                {
                    "id": "placement-id",
                    "service_id": "service-id",
                    "service_slug": "web",
                    "instance_name": "primary",
                    "desired_state": "active",
                    "instance_role": None,
                    "deployment_profile": "demo",
                    "config_schema_version": "1",
                    "config": {},
                    "assignment_source": "manual",
                    "endpoint_id": None,
                }
            ],
            "operational_override": None,
        },
        "actual": {
            "operational_values": {
                field_name: _value_record(field_name)
                for field_name in (
                    "actual_state_policy",
                    "host_os",
                    "connection_path",
                    "connection_endpoint",
                    "connection_address",
                    "ansible_port",
                    "power_control",
                    "is_laptop",
                )
            },
            "operational_finding": None,
            "local_findings": [],
            "production": {
                "state": state,
                "reasons": [],
                "placement_effects": [
                    {"placement_id": "placement-id", "instance_name": "primary", "effect": effect, "reason": reason}
                ],
            },
        },
    }


def _v3_report(nodes: list[dict], generation_id: str, digest: str) -> dict:
    return {
        "schema_version": "3.0",
        "generation_id": generation_id,
        "generated_at": "2026-06-27T12:00:00+00:00",
        "report_path": f"production.reports/{generation_id}.json",
        "deployment_profile_digest": digest,
        "summary": {
            "eligible": 1,
            "included": 1,
            "skipped": 0,
            "out_of_scope": 0,
            "placements": 1,
            "active_placements": 1,
            "inactive_placements": 0,
            "applied_placements": 1,
            "not_applied_placements": 0,
        },
        "nodes": nodes,
    }


def test_production_report_v3_schema_is_closed():
    generation_id = "12345678-1234-5678-9234-567812345678"
    digest = "a" * 64
    report = _v3_report([_v3_node_record(state="included")], generation_id, digest)

    assert validate_production_report_v3(report) is report

    old_report = {**report, "schema_version": "2.0"}
    assert_contract_error("unsupported_report_schema", validate_production_report_v3, old_report)

    partial_report = {**report}
    del partial_report["summary"]
    assert_contract_error("invalid_contract_keys", validate_production_report_v3, partial_report)

    partial_node = [dict(_v3_node_record(state="included"))]
    del partial_node[0]["desired"]
    assert_contract_error(
        "invalid_contract_keys", validate_production_report_v3, _v3_report(partial_node, generation_id, digest)
    )

    duplicate_node_report = _v3_report(
        [_v3_node_record(state="included"), _v3_node_record(state="included")], generation_id, digest
    )
    assert_contract_error("duplicate_node_id", validate_production_report_v3, duplicate_node_report)

    contradiction_report = _v3_report(
        [_v3_node_record(state="included", effect="not_applied", reason="node_skipped")], generation_id, digest
    )
    assert_contract_error(
        "placement_effect_contradicts_node_state", validate_production_report_v3, contradiction_report
    )

    missing_effect_report = _v3_report([_v3_node_record(state="included")], generation_id, digest)
    missing_effect_report["nodes"][0]["actual"]["production"]["placement_effects"] = []
    assert_contract_error("invalid_report_schema", validate_production_report_v3, missing_effect_report)

    unknown_placement_effect_report = _v3_report([_v3_node_record(state="included")], generation_id, digest)
    unknown_placement_effect_report["nodes"][0]["actual"]["production"]["placement_effects"][0]["placement_id"] = "other"
    assert_contract_error(
        "placement_effect_unknown_placement", validate_production_report_v3, unknown_placement_effect_report
    )

    invalid_state_report = _v3_report([_v3_node_record(state="bogus")], generation_id, digest)
    assert_contract_error("invalid_report_schema", validate_production_report_v3, invalid_state_report)
