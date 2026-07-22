"""Ported from nintent's `tests/test_production_inventory.py` (Phase 2 Step 2).

Converted from unittest to pytest functions; fixtures changed from module-level
helpers returning nintent's `ActualFacts` to `nctl_core.sources.actual.ActualFacts`
(same dataclass, ported unchanged in Step 1). Test intent is unchanged.
"""

from __future__ import annotations

import json
import uuid

import pytest
import yaml

from nctl_core.production.composer import (
    LOCAL_COMPOSITION_CODES,
    MERGE_LOCAL_CODES,
    NODE_LOCAL_CODES,
    PLACEMENT_LOCAL_CODES,
    NodeInput,
    PlacementInput,
    ProductionComposition,
    RealizedState,
    compose_production_inventory,
    render_production_inventory_yml,
    render_production_report_json,
)
from nctl_core.production.contract import ContractError
from nctl_core.production.derivation import EndpointCandidate, OperationalOverride
from nctl_core.sources.actual import ActualFacts

GENERATION_ID = "12345678-1234-5678-9234-567812345678"
GENERATED_AT = "2026-06-27T12:00:00+00:00"
DIGEST = "a" * 64
FRESH = "2026-06-27T00:00:00+00:00"
SSH_KNOWN_HOSTS_FILE = "/home/user/.local/state/nctl/ssh/known_hosts"


def _node_id(slug: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"nctl-test-node:{slug}"))


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


def linux_node(slug, *, placements=(), power="none", facts=None, realized_type="device", device_id=None):
    endpoint = EndpointCandidate(
        id=f"endpoint-{slug}", name="primary", endpoint_type="primary", node_slug=slug,
        ip_address="192.168.0.20/24", dns_name=f"{slug}.example.test",
    )
    override = OperationalOverride(id=f"override-{slug}", power_control=power) if power != "none" else None
    realized = None
    if realized_type is not None:
        realized = RealizedState(
            realized_type=realized_type,
            facts=facts if facts is not None else linux_facts(),
            nautobot_device_id=device_id or f"dev-{slug}",
        )
    return NodeInput(
        id=_node_id(slug),
        slug=slug,
        name=slug,
        lifecycle="active",
        node_type="device",
        endpoints=(endpoint,),
        operational_override=override,
        placements=tuple(placements),
        realized=realized,
    )


def haos_node(slug="aghaos", *, placements=()):
    endpoint = EndpointCandidate(
        id="endpoint-haos",
        name="primary",
        endpoint_type="primary",
        node_slug=slug,
        ip_address="192.168.0.20/24",
        dns_name="aghaos.example.test",
    )
    override = OperationalOverride(
        id="override-haos",
        connection_path="local",
        power_control="none",
        declared_host_os="haos",
        local_endpoint_id=endpoint.id,
        ansible_port=2222,
    )
    return NodeInput(
        id=_node_id("aghaos"),
        slug=slug,
        name="HAOS",
        lifecycle="active",
        node_type="device",
        endpoints=(endpoint,),
        operational_override=override,
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
        ssh_known_hosts_file=SSH_KNOWN_HOSTS_FILE,
    )


def ssh_host(composition: ProductionComposition, slug):
    return composition.inventory["all"]["children"]["ssh_hosts"]["hosts"][slug]


def group_hosts(composition: ProductionComposition, group):
    children = composition.inventory["all"]["children"]
    return set(children.get(group, {"hosts": {}})["hosts"])


def node_record(composition: ProductionComposition, slug):
    """Return the report-3.0 node record for `slug` (Phase 4 Decision 2/3)."""

    return next(node for node in composition.report["nodes"] if node["desired"]["node"]["slug"] == slug)


def skip_reasons(composition: ProductionComposition, slug):
    return node_record(composition, slug)["actual"]["production"]["reasons"]


def local_findings(composition: ProductionComposition, slug):
    return node_record(composition, slug)["actual"]["local_findings"]


def placement_effects(composition: ProductionComposition, slug):
    return node_record(composition, slug)["actual"]["production"]["placement_effects"]


def test_ssh_targets_populated_for_every_included_node():
    # fix_sshkey3 Step 2: ResolvedSshTarget is built from this exact
    # composition run's own route/port/identity, one per node actually
    # included in ssh_hosts.
    from nctl_core.ssh_trust import derive_host_key_alias

    node = linux_node("agweb")
    composition = compose([node])

    assert set(composition.ssh_targets) == {"agweb"}
    target = composition.ssh_targets["agweb"]
    assert target.slug == "agweb"
    assert target.desired_node_id == node.id
    assert target.alias == derive_host_key_alias(node.id)
    assert target.route  # resolved via the same pipeline resolve_effective_route uses
    assert target.generation_id == GENERATION_ID


def test_ssh_targets_omits_a_node_skipped_by_local_composition_error():
    # A node with a resolvable-looking source route but skipped from
    # production composition (here: unresolved connection path, a
    # NODE_LOCAL_CODES finding) must never get a ResolvedSshTarget -- a
    # post-regeneration scan must treat it as unreachable, never resolve a
    # route for it some other way.
    bad_node = _bad_unresolved_connection_path()
    good_node = linux_node("agweb")
    composition = compose([bad_node, good_node])

    assert "agbad" not in composition.inventory["all"]["children"]["ssh_hosts"]["hosts"]
    assert "agbad" not in composition.ssh_targets
    assert "agweb" in composition.ssh_targets


def test_ssh_targets_empty_when_no_known_hosts_file_supplied():
    # The drift comparator's internal composition (never rendered to disk)
    # omits ssh_known_hosts_file entirely; no SSH trust vars means no
    # targets either, not a best-effort guess.
    node = linux_node("agweb")
    composition = compose_production_inventory(
        [node], PROFILES, generation_id=GENERATION_ID, generated_at=GENERATED_AT, deployment_profile_digest=DIGEST,
    )
    assert composition.ssh_targets == {}


def not_applied_out_of_scope_entries(composition: ProductionComposition):
    """Every `not_applied`/`node_out_of_scope` placement effect across all nodes --
    the report-3.0 replacement for the old composer-owned `report["drift"]` list,
    which only ever carried `active_placement_not_applied` entries.
    """

    return [
        {"slug": node["desired"]["node"]["slug"], **effect}
        for node in composition.report["nodes"]
        for effect in node["actual"]["production"]["placement_effects"]
        if effect["reason"] == "node_out_of_scope"
    ]


def test_accepted_actual_types_source_is_derived_or_override():
    from nctl_core.production.composer import accepted_actual_types_source

    assert accepted_actual_types_source("device", ["device"]) == "derived"
    assert accepted_actual_types_source("service_host", ["container", "device", "virtual_machine"]) == "derived"
    assert accepted_actual_types_source("device", ["device", "virtual_machine"]) == "override"
    assert accepted_actual_types_source("device", []) == "override"


def test_node_report_record_carries_role_and_accepted_actual_types_source():
    node = linux_node("agweb")
    node = NodeInput(**{**node.__dict__, "role": "web-tier", "accepted_actual_types": ("device", "virtual_machine")})
    result = compose([node])

    identity = node_record(result, "agweb")["desired"]["node"]
    assert identity["role"] == "web-tier"
    assert identity["accepted_actual_types"] == ["device", "virtual_machine"]
    assert identity["accepted_actual_types_source"] == "override"


def test_placement_desired_entry_carries_service_identity_and_assignment_source():
    node = linux_node(
        "agweb",
        placements=[
            PlacementInput(
                "p1", "primary", "web", "1", config={"enabled": True},
                service_id="svc-1", service_slug="web", instance_role="primary", assignment_source="yaml",
                endpoint_id="endpoint-agweb",
            )
        ],
    )
    result = compose([node])

    placement = node_record(result, "agweb")["desired"]["placements"][0]
    assert placement["service_id"] == "svc-1"
    assert placement["service_slug"] == "web"
    assert placement["instance_role"] == "primary"
    assert placement["assignment_source"] == "yaml"
    assert placement["endpoint_id"] == "endpoint-agweb"


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
    assert "nintent_operational_config_id" not in host
    operational_values = node_record(result, "agweb")["actual"]["operational_values"]
    assert operational_values["host_os"] == {
        "value": "linux",
        "source": "derived",
        "source_reference": {
            "kind": "nodeutils_observation",
            "observed_system": "Linux",
            "collected_at": FRESH,
        },
        "override_won": False,
    }
    assert operational_values["ansible_port"]["value"] is None
    assert operational_values["ansible_port"]["source"] == "default"


def test_ssh_trust_vars_are_absent_when_known_hosts_file_omitted():
    node = linux_node("agweb")
    result = compose_production_inventory(
        [node],
        PROFILES,
        generation_id=GENERATION_ID,
        generated_at=GENERATED_AT,
        deployment_profile_digest=DIGEST,
    )
    host = ssh_host(result, "agweb")
    assert "nctl_ssh_host_key_alias" not in host
    assert "ansible_ssh_common_args" not in host


def test_ssh_trust_vars_derive_from_node_id_and_carry_strict_options():
    from nctl_core.ssh_trust import derive_host_key_alias

    node = linux_node("agweb")
    result = compose([node])
    host = ssh_host(result, "agweb")

    expected_alias = derive_host_key_alias(node.id)
    assert host["nctl_ssh_host_key_alias"] == expected_alias
    args = host["ansible_ssh_common_args"]
    assert f"HostKeyAlias={expected_alias}" in args
    assert f"UserKnownHostsFile={SSH_KNOWN_HOSTS_FILE}" in args
    assert "StrictHostKeyChecking=yes" in args
    assert "CheckHostIP=no" in args
    assert "UpdateHostKeys=no" in args


def test_ssh_trust_vars_are_identical_to_bootstrap_for_the_same_node_id_even_when_ansible_host_selects_an_ip():
    """Regression for plan.md Step 4: changing the connection path/selected route must
    never change the stable alias -- production may select an IP while bootstrap
    selects mDNS, but both must carry byte-identical SSH trust host vars."""
    from nctl_core.hosts_intent import export_hosts_intent
    from nctl_core.sources.desired import DesiredEndpoint, DesiredNode

    node = linux_node("agweb")  # connection_path="local", resolves ansible_host to an IP
    production_result = compose([node])
    production_host = ssh_host(production_result, "agweb")

    bootstrap_export = export_hosts_intent(
        [DesiredNode(id=node.id, slug="agweb", name="agweb", lifecycle="active", node_type="device")],
        [
            DesiredEndpoint(
                id="endpoint-agweb-mdns",
                name="primary",
                endpoint_type="primary",
                node_id=node.id,
                node_slug="agweb",
                mdns_name="agweb.local",
            )
        ],
        ssh_known_hosts_file=SSH_KNOWN_HOSTS_FILE,
    )
    bootstrap_host = bootstrap_export.inventory["all"]["children"]["ssh_hosts"]["hosts"]["agweb"]

    assert production_host["nctl_ssh_host_key_alias"] == bootstrap_host["nctl_ssh_host_key_alias"]
    assert production_host["ansible_ssh_common_args"] == bootstrap_host["ansible_ssh_common_args"]


def test_selector_groups_use_observed_system_directly():
    node = linux_node("agmac", power="none", facts=linux_facts(system="Darwin"))
    result = compose([node])

    assert ssh_host(result, "agmac")["host_os"] == "macos"
    assert "agmac" in group_hosts(result, "macos")
    assert group_hosts(result, "linux") == set()
    assert not_applied_out_of_scope_entries(result) == []


def test_wol_power_marks_power_managed():
    result = compose([linux_node("agwol", power="wol")])

    assert "agwol" in group_hosts(result, "power_managed")
    assert ssh_host(result, "agwol")["power_control"] == "wol"


def test_tailscale_connection_exports_tailscale_ip():
    endpoint = EndpointCandidate("endpoint-ts", "ts", "vpn", "agts", ip_address="100.64.0.10/32")
    override = OperationalOverride(
        id="override-ts",
        connection_path="tailscale",
        tailscale_endpoint_id=endpoint.id,
    )
    node = NodeInput(
        _node_id("agts"), "agts", "agts", "active", "device",
        endpoints=(endpoint,), operational_override=override,
        realized=RealizedState("device", linux_facts(), "dev-ts"),
    )
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
    assert skip_reasons(result, "agnodev") == ["no_realized_device"]


def test_virtual_machine_is_unsupported():
    result = compose([linux_node("agvm", realized_type="virtual_machine")])

    assert skip_reasons(result, "agvm") == ["unsupported_actual_type"]


def test_stale_actual_data_is_skipped():
    node = linux_node("agstale", facts=linux_facts(collected_at="2026-06-20T00:00:00+00:00"))
    result = compose([node])

    assert skip_reasons(result, "agstale") == ["stale_actual_data"]


def test_wol_without_mac_is_skipped():
    node = linux_node("agnomac", power="wol", facts=linux_facts(mac=None))
    result = compose([node])

    assert skip_reasons(result, "agnomac") == ["missing_mac_address"]


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


def test_missing_connection_endpoint_skips_only_that_node():
    node = NodeInput(
        id=_node_id("agx"), slug="agx", name="agx", lifecycle="active", node_type="device",
        realized=RealizedState("device", linux_facts(), "dev-x"),
    )
    result = compose([node])

    assert skip_reasons(result, "agx") == ["missing_connection_endpoint"]
    assert local_findings(result, "agx")[0]["code"] == "missing_connection_endpoint"
    assert local_findings(result, "agx")[0]["stage"] == "operational_derivation"
    assert "agx" not in result.inventory["all"]["children"]["ssh_hosts"]["hosts"]


def test_invalid_platform_power_skips_only_that_node():
    result = compose([linux_node("agbad", power="macos_sleep")])

    assert skip_reasons(result, "agbad") == ["invalid_platform_power"]
    error = local_findings(result, "agbad")[0]
    assert error["code"] == "invalid_platform_power"
    assert error["stage"] == "operational_derivation"
    assert error["evidence"]["field"] == "power_control"


def test_conflicting_placement_variables_skip_only_that_node():
    node = linux_node(
        "agconf",
        placements=[
            PlacementInput("p1", "primary", "web", "1", config={"enabled": True}),
            PlacementInput("p2", "secondary", "web", "1", config={"enabled": False}),
        ],
    )
    result = compose([node])

    assert skip_reasons(result, "agconf") == ["conflicting_host_variable"]
    error = local_findings(result, "agconf")[0]
    assert error["code"] == "conflicting_host_variable"
    assert error["stage"] == "host_merge"


def test_unknown_profile_skips_only_that_node():
    node = linux_node("agunk", placements=[PlacementInput("p1", "primary", "missing", "1", config={})])
    result = compose([node])

    assert skip_reasons(result, "agunk") == ["unknown_profile"]
    error = local_findings(result, "agunk")[0]
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


@pytest.mark.parametrize("failure", ["ambiguous_connection_endpoints", "missing_connection_endpoint"])
def test_endpoint_failure_neighbor_does_not_change_healthy_output(failure: str) -> None:
    healthy = linux_node("aggood")
    endpoints = (
        (
            EndpointCandidate("endpoint-z", "z", "management", "agbad", ip_address="192.0.2.2"),
            EndpointCandidate("endpoint-a", "a", "management", "agbad", ip_address="192.0.2.3"),
        )
        if failure == "ambiguous_connection_endpoints"
        else ()
    )
    bad = _custom_node("agbad", endpoints=endpoints)
    alone = compose([healthy])
    mixed = compose([healthy, bad])

    assert ssh_host(mixed, "aggood") == ssh_host(alone, "aggood")
    assert local_findings(mixed, "agbad")[0]["code"] == failure
    assert local_findings(mixed, "agbad")[0]["evidence"]["field"] == "connection_endpoint"


def test_renderers_are_schema_versioned_and_parseable():
    result = compose([linux_node("agweb")])

    rendered_yaml = render_production_inventory_yml(result)
    assert "# schema_version: 3.0\n" in rendered_yaml
    loaded = yaml.safe_load(rendered_yaml)
    assert loaded["all"]["vars"]["nintent_inventory_schema_version"] == "3.0"

    rendered_json = render_production_report_json(result)
    assert rendered_json.endswith("\n")
    assert json.loads(rendered_json)["schema_version"] == "3.0"


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


def _custom_node(slug, *, override_kwargs=None, endpoints=None, placements=(), realized_type="device", facts=None):
    if endpoints is None:
        endpoints = (
            EndpointCandidate(
                id=f"endpoint-{slug}", name="primary", endpoint_type="primary", node_slug=slug,
                ip_address="192.168.9.9/24",
            ),
        )
    override = OperationalOverride(id=f"override-{slug}", **(override_kwargs or {})) if override_kwargs else None
    realized = None
    if realized_type is not None:
        realized = RealizedState(realized_type=realized_type, facts=facts or linux_facts(), nautobot_device_id=f"dev-{slug}")
    return NodeInput(
        id=_node_id(slug), slug=slug, name=slug, lifecycle="active", node_type="device",
        endpoints=tuple(endpoints), operational_override=override,
        placements=tuple(placements), realized=realized,
    )


def _bad_unsupported_observed_host_os():
    return _custom_node(
        "agbad",
        facts=linux_facts(system="FreeBSD"),
    )


def _bad_invalid_platform_power():
    return _custom_node(
        "agbad",
        override_kwargs=dict(power_control="macos_sleep"),
    )


def _bad_endpoint_node_mismatch():
    endpoint = EndpointCandidate(
        id="endpoint-bad", name="primary", endpoint_type="primary",
        node_slug="someone-else", ip_address="192.168.9.9/24",
    )
    return _custom_node(
        "agbad",
        endpoints=(endpoint,),
    )


def _bad_unresolved_connection_path():
    return _custom_node(
        "agbad",
        override_kwargs=dict(connection_path="tailscale"),
    )


def _bad_invalid_connection_address():
    endpoint = EndpointCandidate(
        id="endpoint-ts", name="ts", endpoint_type="vpn", node_slug="agbad", ip_address="not-an-ip"
    )
    return _custom_node(
        "agbad",
        endpoints=(endpoint,),
        override_kwargs=dict(connection_path="tailscale", tailscale_endpoint_id=endpoint.id),
    )


def _bad_unknown_profile():
    return linux_node("agbad", placements=[PlacementInput("p-bad", "primary", "missing", "1", config={})])


def _bad_unsupported_config_schema():
    return linux_node("agbad", placements=[PlacementInput("p-bad", "primary", "web", "99", config={})])


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
        ssh_known_hosts_file=SSH_KNOWN_HOSTS_FILE,
    )

    assert "aggood" in result.inventory["all"]["children"]["ssh_hosts"]["hosts"]
    assert skip_reasons(result, "agbad") == ["invalid_placement_config"]
    assert local_findings(result, "agbad")[0]["code"] == "invalid_placement_config"
    assert local_findings(result, "agbad")[0]["stage"] == "placement_config"


def _bad_unknown_config_key():
    return linux_node("agbad", placements=[PlacementInput("p-bad", "primary", "web", "1", config={"nope": True})])


def _bad_missing_required_config():
    return linux_node("agbad", placements=[PlacementInput("p-bad", "primary", "strict", "1", config={})])


def _bad_invalid_profile_value_type():
    return linux_node("agbad", placements=[PlacementInput("p-bad", "primary", "web", "1", config={"enabled": "nope"})])


def _bad_conflicting_host_variable():
    return linux_node(
        "agbad",
        placements=[
            PlacementInput("p-bad-1", "primary", "web", "1", config={"enabled": True}),
            PlacementInput("p-bad-2", "secondary", "web", "1", config={"enabled": False}),
        ],
    )


_GROUP_C_CASES = {
    "unsupported_observed_host_os": (_bad_unsupported_observed_host_os, PROFILES),
    "invalid_platform_power": (_bad_invalid_platform_power, PROFILES),
    "endpoint_node_mismatch": (_bad_endpoint_node_mismatch, PROFILES),
    "unresolved_connection_path": (_bad_unresolved_connection_path, PROFILES),
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
        ssh_known_hosts_file=SSH_KNOWN_HOSTS_FILE,
    )

    ssh_hosts = result.inventory["all"]["children"]["ssh_hosts"]["hosts"]
    assert "aggood" in ssh_hosts
    assert "agbad" not in ssh_hosts
    for group_hosts_dict in result.inventory["all"]["children"].values():
        assert "agbad" not in group_hosts_dict["hosts"]

    record = node_record(result, "agbad")
    assert record["desired"]["node"]["name"] == bad.name
    assert record["desired"]["node"]["id"] == bad.id
    assert record["actual"]["production"]["state"] == "skipped"
    assert skip_reasons(result, "agbad") == [code]
    assert len(local_findings(result, "agbad")) == 1
    error = local_findings(result, "agbad")[0]
    assert error["code"] == code
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
        ssh_known_hosts_file=SSH_KNOWN_HOSTS_FILE,
    )
    second = compose_production_inventory(
        [good, bad], PROFILES,
        generation_id=GENERATION_ID, generated_at=GENERATED_AT, deployment_profile_digest=DIGEST,
        ssh_known_hosts_file=SSH_KNOWN_HOSTS_FILE,
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
        ssh_known_hosts_file=SSH_KNOWN_HOSTS_FILE,
    )
    assert local_findings(result, "agbad")[0]["code"] == "invalid_connection_address"
    assert local_findings(result, "agbad")[0]["stage"] == "operational_derivation"


def test_group_a_shared_profile_error_still_aborts_globally():
    with pytest.raises(ContractError) as caught:
        compose_production_inventory(
            [linux_node("agweb")],
            {"web": {"group": "web_server", "config_schema_version": "1", "variables": "not-an-object"}},
            generation_id=GENERATION_ID, generated_at=GENERATED_AT, deployment_profile_digest=DIGEST,
            ssh_known_hosts_file=SSH_KNOWN_HOSTS_FILE,
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


# --- Step 1.3: active_placement_not_applied (unapplied intent) -------------


def _planned_node(slug, *, lifecycle="planned", placements=(), node_type="device"):
    return NodeInput(
        id=_node_id(slug), slug=slug, name=slug, lifecycle=lifecycle, node_type=node_type,
        placements=tuple(placements), realized=None,
    )


@pytest.mark.parametrize("lifecycle", ["planned", "deprecated", "retired"])
def test_active_placement_on_ineligible_lifecycle_emits_finding(lifecycle):
    node = _planned_node(
        "agplanned", lifecycle=lifecycle,
        placements=[PlacementInput("p1", "primary", "web", "1", config={"enabled": True})],
    )
    result = compose([node])

    record = node_record(result, "agplanned")
    assert record["actual"]["production"]["state"] == "out_of_scope"
    assert record["desired"]["node"]["lifecycle"] == lifecycle
    entries = [e for e in placement_effects(result, "agplanned") if e["reason"] == "node_out_of_scope"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["placement_id"] == "p1"
    assert entry["effect"] == "not_applied"
    assert record["desired"]["placements"][0]["config"] == {"enabled": True}
    # A planned/deprecated/retired node never enters inventory scope.
    assert "agplanned" not in result.inventory["all"]["children"]["ssh_hosts"]["hosts"]
    assert result.report["summary"]["eligible"] == 0


def test_disabled_placement_on_ineligible_node_produces_no_finding():
    node = _planned_node(
        "agplanned",
        placements=[PlacementInput("p1", "primary", "web", "1", desired_state="disabled", config={"enabled": True})],
    )
    result = compose([node])

    assert not_applied_out_of_scope_entries(result) == []
    assert placement_effects(result, "agplanned")[0]["effect"] == "inactive_by_intent"


def test_empty_config_is_still_evidence_for_unapplied_placement():
    node = _planned_node(
        "agplanned",
        placements=[PlacementInput("p1", "primary", "home_assistant", "1", config={})],
    )
    result = compose([node])

    entries = [e for e in placement_effects(result, "agplanned") if e["reason"] == "node_out_of_scope"]
    assert len(entries) == 1
    assert node_record(result, "agplanned")["desired"]["placements"][0]["config"] == {}


def test_multiple_placements_on_one_ineligible_node_each_get_a_finding():
    node = _planned_node(
        "agplanned",
        placements=[
            PlacementInput("p1", "web", "web", "1", config={"enabled": True}),
            PlacementInput("p2", "db", "db", "1", config={"port": 5432}),
        ],
    )
    result = compose([node])

    entries = sorted(
        (e for e in placement_effects(result, "agplanned") if e["reason"] == "node_out_of_scope"),
        key=lambda e: e["instance_name"],
    )
    assert [e["instance_name"] for e in entries] == ["db", "web"]


def test_production_eligible_control_node_gets_no_unapplied_finding():
    node = linux_node("agactive", placements=[PlacementInput("p1", "primary", "web", "1", config={"enabled": True})])
    result = compose([node])

    assert not_applied_out_of_scope_entries(result) == []


def test_node_type_only_ineligibility_is_out_of_scope_in_the_report():
    # A container is out of production scope for a node_type reason, not a
    # lifecycle reason -- report 3.0 (Phase 4 Decision 2) still surfaces it
    # uniformly as `out_of_scope` with a `node_out_of_scope` placement effect,
    # covering every ineligibility reason rather than only the lifecycle gate
    # `unapplied_placement_findings` (the older, narrower drift-only helper
    # still used when profiles are unavailable) is scoped to.
    node = _planned_node(
        "agcontainer", lifecycle="active", node_type="container",
        placements=[PlacementInput("p1", "primary", "web", "1", config={"enabled": True})],
    )
    result = compose([node])

    record = node_record(result, "agcontainer")
    assert record["actual"]["production"]["state"] == "out_of_scope"
    assert placement_effects(result, "agcontainer") == [
        {"placement_id": "p1", "instance_name": "primary", "effect": "not_applied", "reason": "node_out_of_scope"}
    ]
    # The older lifecycle-only helper still does not fire for this node.
    assert not_applied_out_of_scope_entries(result)  # composer's own report *does* cover it
    from nctl_core.production.composer import unapplied_placement_findings

    assert unapplied_placement_findings([node]) == []


def test_unapplied_placement_findings_helper_is_deterministically_ordered():
    from nctl_core.production.composer import unapplied_placement_findings

    node_b = _planned_node("agb", placements=[PlacementInput("p2", "z-instance", "web", "1", config={})])
    node_a = _planned_node("aga", placements=[PlacementInput("p1", "a-instance", "web", "1", config={})])
    entries = unapplied_placement_findings([node_b, node_a])
    assert [e["desired_node_slug"] for e in entries] == ["aga", "agb"]


def test_unapplied_placement_findings_does_not_touch_profiles():
    from nctl_core.production.composer import unapplied_placement_findings

    node = _planned_node("agplanned", placements=[PlacementInput("p1", "primary", "totally-unknown-profile", "1", config={})])
    # No ContractError -- the helper never validates against a profile map at all.
    entries = unapplied_placement_findings([node])
    assert entries[0]["placement"]["deployment_profile"] == "totally-unknown-profile"
