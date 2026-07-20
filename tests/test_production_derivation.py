from __future__ import annotations

import pytest

from nctl_core.production.derivation import (
    DerivationFailure,
    EndpointCandidate,
    OperationalOverride,
    resolve_operational_values,
)
from nctl_core.sources.actual import ActualFacts


GENERATED_AT = "2026-07-20T00:00:00Z"


def _facts(system: str = "Linux", *, local_ip: str | None = "192.0.2.10") -> ActualFacts:
    return ActualFacts(
        observed_system=system,
        local_ip=local_ip,
        mac_address=None,
        network_interface=None,
        collected_at="2026-07-19T23:00:00Z",
        inventory_source=None,
    )


def _endpoint(
    endpoint_id: str = "ep-1",
    *,
    name: str = "primary",
    endpoint_type: str = "primary",
    ip_address: str | None = "192.0.2.20",
    dns_name: str | None = None,
    mdns_name: str | None = None,
) -> EndpointCandidate:
    return EndpointCandidate(
        id=endpoint_id,
        name=name,
        endpoint_type=endpoint_type,
        node_slug="node-a",
        ip_address=ip_address,
        dns_name=dns_name,
        mdns_name=mdns_name,
    )


def _resolve(
    *,
    endpoints: tuple[EndpointCandidate, ...] = (_endpoint(),),
    override: OperationalOverride | None = None,
    facts: ActualFacts | None = None,
    realized_type: str | None = "device",
):
    return resolve_operational_values(
        node_id="node-id",
        node_slug="node-a",
        endpoints=endpoints,
        override=override,
        realized_type=realized_type,
        facts=facts or _facts(),
        generated_at=GENERATED_AT,
    )


@pytest.mark.parametrize(
    ("system", "expected_os"),
    [("Linux", "linux"), ("Darwin", "macos")],
)
def test_observed_platform_and_exact_provenance_contract(system: str, expected_os: str) -> None:
    values = _resolve(facts=_facts(system))

    assert values.host_os.as_dict() == {
        "value": expected_os,
        "source": "derived",
        "source_reference": {
            "kind": "nodeutils_observation",
            "observed_system": system,
            "collected_at": "2026-07-19T23:00:00Z",
        },
        "override_won": False,
    }
    assert set(values.as_dict()) == {
        "actual_state_policy",
        "host_os",
        "connection_path",
        "connection_endpoint",
        "connection_address",
        "ansible_port",
        "power_control",
        "is_laptop",
    }


@pytest.mark.parametrize(
    ("endpoints", "expected_id"),
    [
        ((_endpoint("only", endpoint_type="management"),), "only"),
        (
            (
                _endpoint("z-secondary", endpoint_type="management"),
                _endpoint("a-primary", endpoint_type="primary"),
            ),
            "a-primary",
        ),
    ],
)
def test_endpoint_scenario_matrix_selects_single_or_unique_primary(endpoints, expected_id: str) -> None:
    values = _resolve(endpoints=endpoints, facts=_facts(local_ip=None))
    assert values.connection_endpoint.source_reference["id"] == expected_id
    assert values.connection_path.value == "local"


@pytest.mark.parametrize(
    ("endpoints", "code"),
    [
        ((), "missing_connection_endpoint"),
        (
            (
                _endpoint("z", endpoint_type="management"),
                _endpoint("a", endpoint_type="management"),
            ),
            "ambiguous_connection_endpoints",
        ),
    ],
)
def test_endpoint_scenario_matrix_fails_locally_and_orders_evidence(endpoints, code: str) -> None:
    with pytest.raises(DerivationFailure) as raised:
        _resolve(endpoints=endpoints)
    assert raised.value.code == code
    if code == "ambiguous_connection_endpoints":
        assert [item["id"] for item in raised.value.evidence["candidates"]] == ["a", "z"]


def test_declared_haos_and_forced_tailscale_are_override_driven() -> None:
    vpn = _endpoint("vpn", endpoint_type="vpn", ip_address="100.64.0.10")
    override = OperationalOverride(
        id="override-1",
        declared_host_os="haos",
        connection_path="tailscale",
        tailscale_endpoint_id="vpn",
        ansible_port=2222,
    )

    values = _resolve(endpoints=(vpn,), override=override, facts=None, realized_type=None)

    assert values.actual_state_policy.value == "declared"
    assert values.host_os.as_dict()["override_won"] is True
    assert values.connection_path.source == "override"
    assert values.connection_address.value == "100.64.0.10"
    assert values.ansible_port.value == 2222


@pytest.mark.parametrize(
    ("facts", "realized_type", "expected_code"),
    [
        (None, None, "no_realized_device"),
        (
            ActualFacts("Linux", None, None, None, "bad", None),
            "device",
            "invalid_actual_timestamp",
        ),
        (
            ActualFacts("Linux", None, None, None, "2026-01-01T00:00:00Z", None),
            "device",
            "stale_actual_data",
        ),
        (_facts("Windows"), "device", "unsupported_observed_host_os"),
    ],
)
def test_observation_failure_matrix(facts, realized_type: str | None, expected_code: str) -> None:
    with pytest.raises(DerivationFailure) as raised:
        resolve_operational_values(
            node_id="node-id",
            node_slug="node-a",
            endpoints=(_endpoint(),),
            override=None,
            realized_type=realized_type,
            facts=facts,
            generated_at=GENERATED_AT,
        )
    assert raised.value.code == expected_code


def test_safe_defaults_are_closed_records() -> None:
    values = _resolve()
    assert values.ansible_port.as_dict() == {
        "value": None,
        "source": "default",
        "source_reference": {"kind": "ansible_default"},
        "override_won": False,
    }
    assert values.power_control.value == "none"
    assert values.is_laptop.value is False
