"""Ported from nintent's `tests/test_production_inventory.py` (Phase 2 Step 2).

Converted from unittest to pytest functions; fixtures changed from module-level
helpers returning nintent's `ActualFacts` to `nctl_core.sources.actual.ActualFacts`
(same dataclass, ported unchanged in Step 1). Test intent is unchanged.
"""

from __future__ import annotations

import json

import pytest
import yaml

from nctl_core.production.composer import (
    LOCAL_COMPOSITION_CODES,
    MERGE_LOCAL_CODES,
    NODE_LOCAL_CODES,
    PLACEMENT_LOCAL_CODES,
    EndpointInput,
    NodeInput,
    OperationalConfigInput,
    PlacementInput,
    ProductionComposition,
    RealizedState,
    compose_production_inventory,
    render_production_inventory_yml,
    render_production_report_json,
)
from nctl_core.production.contract import ContractError
from nctl_core.sources.actual import ActualFacts

GENERATION_ID = "12345678-1234-5678-9234-567812345678"
GENERATED_AT = "2026-06-27T12:00:00+00:00"
DIGEST = "a" * 64
FRESH = "2026-06-27T00:00:00+00:00"

PROFILES = {
    "web": {
        "group": "web_server",
        "config_schema_version": "1",
        "variables": {
            "enabled": {"ansible_variable": "web_enabled", "type": "boolean", "required": False},
            "peers": {"ansible_variable": "web_peers", "type": "list", "items": "string", "required": False},
        },
    },
    "db": {
        "group": "db_server",
        "config_schema_version": "1",
        "variables": {
            "port": {"ansible_variable": "db_port", "type": "integer", "required": False},
        },
    },
    "home_assistant": {
        "group": "haos_server",
        "config_schema_version": "1",
        "variables": {},
    },
}


def linux_facts(*, collected_at=FRESH, mac="aa:bb:cc:dd:ee:ff", iface="eth0", local_ip="192.168.0.10", system="Linux"):
    return ActualFacts(
        observed_system=system,
        local_ip=local_ip,
        mac_address=mac,
        network_interface=iface,
        collected_at=collected_at,
        inventory_source="nodeutils",
    )


def linux_node(slug, *, placements=(), power="none", expected="linux", facts=None, realized_type="device", device_id=None):
    op = OperationalConfigInput(
        id=f"op-{slug}",
        actual_state_policy="required",
        connection_path="local",
        power_control=power,
        expected_host_os=expected,
    )
    realized = None
    if realized_type is not None:
        realized = RealizedState(
            realized_type=realized_type,
            facts=facts if facts is not None else linux_facts(),
            nautobot_device_id=device_id or f"dev-{slug}",
        )
    return NodeInput(
        id=f"node-{slug}",
        slug=slug,
        name=slug,
        lifecycle="active",
        node_type="device",
        operational_config=op,
        placements=tuple(placements),
        realized=realized,
    )


def haos_node(slug="aghaos", *, placements=()):
    endpoint = EndpointInput(
        name="primary",
        endpoint_type="primary",
        node_slug=slug,
        ip_address="192.168.0.20/24",
        dns_name="aghaos.example.test",
    )
    op = OperationalConfigInput(
        id="op-haos",
        actual_state_policy="declared",
        connection_path="local",
        power_control="none",
        declared_host_os="haos",
        local_endpoint=endpoint,
        ansible_port=2222,
    )
    return NodeInput(
        id="node-haos",
        slug=slug,
        name="HAOS",
        lifecycle="active",
        node_type="device",
        operational_config=op,
        placements=tuple(placements),
        realized=None,
    )


def compose(nodes):
    return compose_production_inventory(
        nodes,
        PROFILES,
        generation_id=GENERATION_ID,
        generated_at=GENERATED_AT,
        deployment_profile_digest=DIGEST,
    )


def ssh_host(composition: ProductionComposition, slug):
    return composition.inventory["all"]["children"]["ssh_hosts"]["hosts"][slug]


def group_hosts(composition: ProductionComposition, group):
    children = composition.inventory["all"]["children"]
    return set(children.get(group, {"hosts": {}})["hosts"])


def test_linux_node_joins_actual_facts_and_service_group():
    node = linux_node("agweb", placements=[PlacementInput("p1", "primary", "web", "1", config={"enabled": True})])
    result = compose([node])

    host = ssh_host(result, "agweb")
    assert host["host_os"] == "linux"
    assert host["local_ip"] == "192.168.0.10"
    assert host["mac_address"] == "aa:bb:cc:dd:ee:ff"
    assert host["network_interface"] == "eth0"
    assert host["nautobot_device_id"] == "dev-agweb"
    assert host["web_enabled"] is True
    assert host["nintent_active_placement_ids"] == ["p1"]
    assert "agweb" in group_hosts(result, "linux")
    assert "agweb" in group_hosts(result, "web_server")
    assert result.report["summary"]["active_placements"] == 1
    assert result.report["summary"]["included"] == 1


def test_selector_groups_use_observed_system_not_expected():
    # expected linux, observed Darwin -> exported macos with drift, in macos group.
    node = linux_node("agmac", power="none", expected="linux", facts=linux_facts(system="Darwin"))
    result = compose([node])

    assert ssh_host(result, "agmac")["host_os"] == "macos"
    assert "agmac" in group_hosts(result, "macos")
    assert group_hosts(result, "linux") == set()
    assert result.report["drift"][0]["code"] == "desired_actual_os_mismatch"
    assert result.report["drift"][0]["desired_node_slug"] == "agmac"


def test_wol_power_marks_power_managed():
    result = compose([linux_node("agwol", power="wol")])

    assert "agwol" in group_hosts(result, "power_managed")
    assert ssh_host(result, "agwol")["power_control"] == "wol"


def test_tailscale_connection_exports_tailscale_ip():
    op = OperationalConfigInput(
        id="op-ts",
        actual_state_policy="required",
        connection_path="tailscale",
        expected_host_os="linux",
        tailscale_endpoint=EndpointInput("ts", "vpn", "agts", ip_address="100.64.0.10/32"),
    )
    node = NodeInput("node-ts", "agts", "agts", "active", "device", op, (), RealizedState("device", linux_facts(), "dev-ts"))
    result = compose([node])

    host = ssh_host(result, "agts")
    assert host["connection_path"] == "tailscale"
    assert host["tailscale_ip"] == "100.64.0.10"


def test_haos_composed_without_realized_object():
    result = compose([haos_node()])

    host = ssh_host(result, "aghaos")
    assert host["host_os"] == "haos"
    assert host["local_ip"] == "192.168.0.20"
    assert host["ansible_port"] == 2222
    assert "mac_address" not in host
    assert "network_interface" not in host
    assert "nautobot_device_id" not in host
    assert "aghaos" in group_hosts(result, "haos")
    assert group_hosts(result, "power_managed") == set()


def test_haos_declared_node_joins_service_group():
    # The declared-node path must still place HAOS into its service group so
    # the home-assistant deployment play can target it without nodeutils data.
    node = haos_node(placements=[PlacementInput("p1", "primary", "home_assistant", "1", config={})])
    result = compose([node])

    assert "aghaos" in group_hosts(result, "haos_server")
    assert ssh_host(result, "aghaos")["nintent_active_placement_ids"] == ["p1"]
    assert result.report["summary"]["active_placements"] == 1


def test_linux_macos_and_declared_haos_compose_together():
    nodes = [
        linux_node(
            "aglinux",
            placements=[PlacementInput("p-linux", "primary", "web", "1", config={"enabled": True})],
        ),
        linux_node(
            "agmac",
            expected="macos",
            facts=linux_facts(
                system="Darwin",
                local_ip="192.168.0.11",
                mac="11:22:33:44:55:66",
                iface="en0",
            ),
        ),
        haos_node(
            placements=[PlacementInput("p-haos", "primary", "home_assistant", "1", config={})],
        ),
    ]

    result = compose(nodes)

    assert result.report["summary"]["included"] == 3
    assert group_hosts(result, "ssh_hosts") == {"aglinux", "agmac", "aghaos"}
    assert group_hosts(result, "linux") == {"aglinux"}
    assert group_hosts(result, "macos") == {"agmac"}
    assert group_hosts(result, "haos") == {"aghaos"}
    assert group_hosts(result, "web_server") == {"aglinux"}
    assert group_hosts(result, "haos_server") == {"aghaos"}
    for hostname in group_hosts(result, "ssh_hosts"):
        assert "package_manager" not in ssh_host(result, hostname)


def test_missing_realized_device_is_skipped():
    result = compose([linux_node("agnodev", realized_type=None)])

    assert result.inventory["all"]["children"]["ssh_hosts"]["hosts"] == {}
    assert result.report["skipped"][0]["reasons"] == ["no_realized_device"]


def test_virtual_machine_is_unsupported():
    result = compose([linux_node("agvm", realized_type="virtual_machine")])

    assert result.report["skipped"][0]["reasons"] == ["unsupported_actual_type"]


def test_stale_actual_data_is_skipped():
    node = linux_node("agstale", facts=linux_facts(collected_at="2026-06-20T00:00:00+00:00"))
    result = compose([node])

    assert result.report["skipped"][0]["reasons"] == ["stale_actual_data"]


def test_wol_without_mac_is_skipped():
    node = linux_node("agnomac", power="wol", facts=linux_facts(mac=None))
    result = compose([node])

    assert result.report["skipped"][0]["reasons"] == ["missing_mac_address"]


def test_skipped_host_placement_does_not_create_dangling_group():
    node = linux_node(
        "agskip",
        realized_type=None,
        placements=[PlacementInput("p1", "primary", "web", "1", config={"enabled": True})],
    )
    result = compose([node])

    assert group_hosts(result, "web_server") == set()
    assert result.report["summary"]["inactive_placements"] == 1
    assert result.report["summary"]["active_placements"] == 0


def test_missing_operational_config_skips_only_that_node():
    node = NodeInput("node-x", "agx", "agx", "active", "device", None, (), None)
    result = compose([node])

    assert result.report["skipped"][0]["reasons"] == ["missing_operational_config"]
    assert result.report["errors"][0]["code"] == "missing_operational_config"
    assert result.report["errors"][0]["stage"] == "operational_config"
    assert "agx" not in result.inventory["all"]["children"]["ssh_hosts"]["hosts"]


def test_invalid_platform_power_skips_only_that_node():
    result = compose([linux_node("agbad", power="macos_sleep")])

    assert result.report["skipped"][0]["reasons"] == ["invalid_platform_power"]
    error = result.report["errors"][0]
    assert error["code"] == "invalid_platform_power"
    assert error["stage"] == "platform_policy"


def test_conflicting_placement_variables_skip_only_that_node():
    node = linux_node(
        "agconf",
        placements=[
            PlacementInput("p1", "primary", "web", "1", config={"enabled": True}),
            PlacementInput("p2", "secondary", "web", "1", config={"enabled": False}),
        ],
    )
    result = compose([node])

    assert result.report["skipped"][0]["reasons"] == ["conflicting_host_variable"]
    error = result.report["errors"][0]
    assert error["code"] == "conflicting_host_variable"
    assert error["stage"] == "host_merge"


def test_unknown_profile_skips_only_that_node():
    node = linux_node("agunk", placements=[PlacementInput("p1", "primary", "missing", "1", config={})])
    result = compose([node])

    assert result.report["skipped"][0]["reasons"] == ["unknown_profile"]
    error = result.report["errors"][0]
    assert error["code"] == "unknown_profile"
    assert error["stage"] == "placement_config"
    assert error["evidence"]["placement"]["deployment_profile"] == "missing"
    assert error["evidence"]["placement"]["id"] == "p1"


def test_multiple_services_on_one_node():
    node = linux_node(
        "agmulti",
        placements=[
            PlacementInput("p1", "web", "web", "1", config={"enabled": True}),
            PlacementInput("p2", "db", "db", "1", config={"port": 5432}),
        ],
    )
    result = compose([node])

    host = ssh_host(result, "agmulti")
    assert host["web_enabled"] is True
    assert host["db_port"] == 5432
    assert "agmulti" in group_hosts(result, "web_server")
    assert "agmulti" in group_hosts(result, "db_server")
    assert result.report["summary"]["active_placements"] == 2


def test_multiple_instances_of_one_service():
    node = linux_node(
        "aginst",
        placements=[
            PlacementInput("p-b", "replica", "web", "1", config={"enabled": True}),
            PlacementInput("p-a", "primary", "web", "1", config={"enabled": True}),
        ],
    )
    result = compose([node])

    assert ssh_host(result, "aginst")["nintent_active_placement_ids"] == ["p-a", "p-b"]
    assert group_hosts(result, "web_server") == {"aginst"}
    assert result.report["summary"]["active_placements"] == 2


def test_disabled_placement_is_inactive():
    node = linux_node(
        "agdis",
        placements=[PlacementInput("p1", "primary", "web", "1", desired_state="disabled", config={"enabled": True})],
    )
    result = compose([node])

    assert "web_enabled" not in ssh_host(result, "agdis")
    assert group_hosts(result, "web_server") == set()
    assert result.report["summary"]["active_placements"] == 0
    assert result.report["summary"]["inactive_placements"] == 1


def test_ineligible_nodes_are_out_of_scope():
    planned = linux_node("agplanned")
    planned = NodeInput(**{**planned.__dict__, "lifecycle": "planned"})
    container = linux_node("agcontainer")
    container = NodeInput(**{**container.__dict__, "node_type": "container"})
    included = linux_node("agok")

    result = compose([planned, container, included])

    assert set(result.inventory["all"]["children"]["ssh_hosts"]["hosts"]) == {"agok"}
    assert result.report["summary"]["eligible"] == 1


def test_output_is_byte_stable():
    nodes = [
        linux_node("agweb", placements=[PlacementInput("p1", "primary", "web", "1", config={"enabled": True})]),
        haos_node(),
        linux_node("agwol", power="wol"),
    ]
    first = compose(list(nodes))
    second = compose(list(nodes))

    assert render_production_inventory_yml(first) == render_production_inventory_yml(second)
    assert render_production_report_json(first) == render_production_report_json(second)


def test_renderers_are_schema_versioned_and_parseable():
    result = compose([linux_node("agweb")])

    rendered_yaml = render_production_inventory_yml(result)
    assert "# schema_version: 1.0\n" in rendered_yaml
    loaded = yaml.safe_load(rendered_yaml)
    assert loaded["all"]["vars"]["nintent_inventory_schema_version"] == "1.0"

    rendered_json = render_production_report_json(result)
    assert rendered_json.endswith("\n")
    assert json.loads(rendered_json)["schema_version"] == "1.0"


# --- Step 1.6: full Group C isolation matrix -------------------------------
#
# Every case pairs one healthy node with one bad node built to trigger exactly
# one Group C code, and asserts the healthy node composes normally while the
# bad node is skipped with a matching skipped/errors pair -- never a global
# ContractError.

_REQUIRED_PROFILE = {
    "strict": {
        "group": "strict_server",
        "config_schema_version": "1",
        "variables": {
            "required_key": {"ansible_variable": "required_key", "type": "string", "required": True},
        },
    },
}


def _custom_node(slug, *, op_kwargs, placements=(), realized_type="device", facts=None):
    op = OperationalConfigInput(id=f"op-{slug}", **op_kwargs)
    realized = None
    if realized_type is not None:
        realized = RealizedState(realized_type=realized_type, facts=facts or linux_facts(), nautobot_device_id=f"dev-{slug}")
    return NodeInput(
        id=f"node-{slug}", slug=slug, name=slug, lifecycle="active", node_type="device",
        operational_config=op, placements=tuple(placements), realized=realized,
    )


def _bad_missing_operational_config():
    return NodeInput("node-bad", "agbad", "agbad", "active", "device", None, (), None)


def _bad_invalid_actual_state_policy():
    return _custom_node(
        "agbad",
        op_kwargs=dict(
            actual_state_policy="required", connection_path="local",
            expected_host_os="linux", declared_host_os="haos",
        ),
    )


def _bad_unsupported_observed_host_os():
    return _custom_node(
        "agbad",
        op_kwargs=dict(actual_state_policy="required", connection_path="local", expected_host_os="linux"),
        facts=linux_facts(system="FreeBSD"),
    )


def _bad_invalid_platform_power():
    return _custom_node(
        "agbad",
        op_kwargs=dict(
            actual_state_policy="required", connection_path="local",
            expected_host_os="linux", power_control="macos_sleep",
        ),
    )


def _bad_endpoint_node_mismatch():
    endpoint = EndpointInput(name="primary", endpoint_type="primary", node_slug="someone-else", ip_address="192.168.9.9/24")
    return _custom_node(
        "agbad",
        op_kwargs=dict(
            actual_state_policy="required", connection_path="local",
            expected_host_os="linux", local_endpoint=endpoint,
        ),
    )


def _bad_unresolved_connection_path():
    return _custom_node(
        "agbad",
        op_kwargs=dict(actual_state_policy="required", connection_path="tailscale", expected_host_os="linux"),
    )


def _bad_invalid_connection_path():
    return _custom_node(
        "agbad",
        op_kwargs=dict(actual_state_policy="required", connection_path="bogus", expected_host_os="linux"),
    )


def _bad_invalid_connection_address():
    endpoint = EndpointInput(name="ts", endpoint_type="vpn", node_slug="agbad", ip_address="not-an-ip")
    return _custom_node(
        "agbad",
        op_kwargs=dict(
            actual_state_policy="required", connection_path="tailscale",
            expected_host_os="linux", tailscale_endpoint=endpoint,
        ),
    )


def _bad_unknown_profile():
    return linux_node("agbad", placements=[PlacementInput("p1", "primary", "missing", "1", config={})])


def _bad_unsupported_config_schema():
    return linux_node("agbad", placements=[PlacementInput("p1", "primary", "web", "99", config={})])


def test_invalid_placement_config_is_localized_when_raised(monkeypatch):
    import nctl_core.production.composer as composer_mod

    def _raise_invalid_config(*args, **kwargs):
        raise ContractError("invalid_placement_config", "placement config must be an object")

    monkeypatch.setattr(composer_mod, "map_placement_config", _raise_invalid_config)
    good = linux_node("aggood")
    bad = linux_node("agbad", placements=[PlacementInput("p1", "primary", "web", "1", config={"enabled": True})])

    result = compose_production_inventory(
        [good, bad], PROFILES,
        generation_id=GENERATION_ID, generated_at=GENERATED_AT, deployment_profile_digest=DIGEST,
    )

    assert "aggood" in result.inventory["all"]["children"]["ssh_hosts"]["hosts"]
    assert result.report["skipped"][0]["reasons"] == ["invalid_placement_config"]
    assert result.report["errors"][0]["code"] == "invalid_placement_config"
    assert result.report["errors"][0]["stage"] == "placement_config"


def _bad_unknown_config_key():
    return linux_node("agbad", placements=[PlacementInput("p1", "primary", "web", "1", config={"nope": True})])


def _bad_missing_required_config():
    return linux_node("agbad", placements=[PlacementInput("p1", "primary", "strict", "1", config={})])


def _bad_invalid_profile_value_type():
    return linux_node("agbad", placements=[PlacementInput("p1", "primary", "web", "1", config={"enabled": "nope"})])


def _bad_conflicting_host_variable():
    return linux_node(
        "agbad",
        placements=[
            PlacementInput("p1", "primary", "web", "1", config={"enabled": True}),
            PlacementInput("p2", "secondary", "web", "1", config={"enabled": False}),
        ],
    )


_GROUP_C_CASES = {
    "missing_operational_config": (_bad_missing_operational_config, PROFILES),
    "invalid_actual_state_policy": (_bad_invalid_actual_state_policy, PROFILES),
    "unsupported_observed_host_os": (_bad_unsupported_observed_host_os, PROFILES),
    "invalid_platform_power": (_bad_invalid_platform_power, PROFILES),
    "endpoint_node_mismatch": (_bad_endpoint_node_mismatch, PROFILES),
    "unresolved_connection_path": (_bad_unresolved_connection_path, PROFILES),
    "invalid_connection_path": (_bad_invalid_connection_path, PROFILES),
    "invalid_connection_address": (_bad_invalid_connection_address, PROFILES),
    "unknown_profile": (_bad_unknown_profile, PROFILES),
    "unsupported_config_schema": (_bad_unsupported_config_schema, PROFILES),
    # invalid_placement_config is exercised separately below: composer always
    # calls dict(placement.config) before map_placement_config, so a typed
    # PlacementInput can never carry a non-mapping config through this path;
    # the code stays in PLACEMENT_LOCAL_CODES for any future direct caller.
    "unknown_config_key": (_bad_unknown_config_key, PROFILES),
    "missing_required_config": (_bad_missing_required_config, {**PROFILES, **_REQUIRED_PROFILE}),
    "invalid_profile_value_type": (_bad_invalid_profile_value_type, PROFILES),
    "conflicting_host_variable": (_bad_conflicting_host_variable, PROFILES),
}


def test_group_c_matrix_covers_every_declared_local_code():
    assert set(_GROUP_C_CASES) == LOCAL_COMPOSITION_CODES - {"invalid_placement_config"}
    assert "invalid_placement_config" in PLACEMENT_LOCAL_CODES
    assert LOCAL_COMPOSITION_CODES == NODE_LOCAL_CODES | PLACEMENT_LOCAL_CODES | MERGE_LOCAL_CODES


@pytest.mark.parametrize("code", sorted(_GROUP_C_CASES))
def test_group_c_failure_skips_only_the_bad_node(code):
    build_bad, profiles = _GROUP_C_CASES[code]
    good = linux_node("aggood", placements=[PlacementInput("p1", "primary", "web", "1", config={"enabled": True})])
    bad = build_bad()

    result = compose_production_inventory(
        [good, bad], profiles,
        generation_id=GENERATION_ID, generated_at=GENERATED_AT, deployment_profile_digest=DIGEST,
    )

    ssh_hosts = result.inventory["all"]["children"]["ssh_hosts"]["hosts"]
    assert "aggood" in ssh_hosts
    assert "agbad" not in ssh_hosts
    for group_hosts_dict in result.inventory["all"]["children"].values():
        assert "agbad" not in group_hosts_dict["hosts"]

    assert result.report["skipped"] == [
        {
            "item_type": "desired_node",
            "desired_node": bad.name,
            "desired_node_slug": "agbad",
            "desired_node_id": bad.id,
            "reasons": [code],
        }
    ]
    assert len(result.report["errors"]) == 1
    error = result.report["errors"][0]
    assert error["code"] == code
    assert error["desired_node_slug"] == "agbad"
    assert error["severity"] == "error"
    assert error["stage"]
    assert error["message"]

    # The healthy node's own placement config/group membership survives intact.
    assert ssh_hosts["aggood"]["web_enabled"] is True
    assert "aggood" in group_hosts(result, "web_server")


def test_group_c_output_is_byte_stable_across_runs():
    good = linux_node("aggood")
    bad = _bad_unknown_profile()
    first = compose_production_inventory(
        [good, bad], PROFILES,
        generation_id=GENERATION_ID, generated_at=GENERATED_AT, deployment_profile_digest=DIGEST,
    )
    second = compose_production_inventory(
        [good, bad], PROFILES,
        generation_id=GENERATION_ID, generated_at=GENERATED_AT, deployment_profile_digest=DIGEST,
    )
    assert render_production_report_json(first) == render_production_report_json(second)


def test_invalid_connection_address_per_node_call_site_is_local_not_global():
    # contract.py's sole `invalid_connection_address` raise site (_normalize_ip)
    # is reached only through resolve_connection_variables, which the composer
    # calls once per node inside _compose_host -- there is no separate
    # document-level call site in the current pipeline, so this is the one
    # case Phase 0 flagged as needing to stay local (as opposed to a
    # hypothetical Group B document-level normalization, which does not
    # exist today).
    result = compose_production_inventory(
        [_bad_invalid_connection_address()], PROFILES,
        generation_id=GENERATION_ID, generated_at=GENERATED_AT, deployment_profile_digest=DIGEST,
    )
    assert result.report["errors"][0]["code"] == "invalid_connection_address"
    assert result.report["errors"][0]["stage"] == "connection"


def test_group_a_shared_profile_error_still_aborts_globally():
    with pytest.raises(ContractError) as caught:
        compose_production_inventory(
            [linux_node("agweb")],
            {"web": {"group": "web_server", "config_schema_version": "1", "variables": "not-an-object"}},
            generation_id=GENERATION_ID, generated_at=GENERATED_AT, deployment_profile_digest=DIGEST,
        )
    assert caught.value.code == "invalid_profile_variables"


def test_group_b_final_output_error_still_aborts_globally(monkeypatch):
    import nctl_core.production.composer as composer_mod

    def _broken_document(*args, **kwargs):
        raise ContractError("invalid_inventory_schema", "forced for test")

    monkeypatch.setattr(composer_mod, "validate_production_inventory_document", _broken_document)
    with pytest.raises(ContractError) as caught:
        compose([linux_node("agweb")])
    assert caught.value.code == "invalid_inventory_schema"
