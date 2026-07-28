from __future__ import annotations

import json
from pathlib import Path

import pytest

from nctl_core.compute import contract
from nctl_core.sources import desired


FIXTURE = Path(__file__).parent / "fixtures/compute_conformance.json"


def _endpoint(attributes):
    defaults = {
        "id": "endpoint", "name": "endpoint", "node_id": "node", "node_slug": "node",
        "dnsmasq_record_type": "host_record",
    }
    defaults.update(attributes)
    return desired.DesiredEndpoint(**defaults)


def _run(case):
    data, rule = case["input"], case["rule"]
    if rule == "select_compute_primary_endpoint":
        selected, code = contract.select_compute_primary_endpoint([_endpoint(item) for item in data["endpoints"]])
        return {"selected": selected is not None, "code": code}
    if rule in {"endpoint_has_usable_ip", "endpoint_satisfies_compute_address_contract"}:
        name = "endpoint_has_usable_ip" if rule == "endpoint_has_usable_ip" else "endpoint_satisfies_compute_address_contract"
        return getattr(contract, name)(_endpoint(data["endpoint"]))
    if rule == "validate_instance_config":
        return contract.validate_instance_config(data["value"], instance_kind=data["instance_kind"])
    if rule == "link_source_pairing_is_valid":
        return bool(data["link_present"]) == bool(data["source"])
    if rule == "validate_link_source":
        return contract.validate_link_source(data["value"], path="realized_vm_source")
    if rule == "effective_lifecycle":
        return contract.effective_lifecycle(data["node"], data["platform"])
    if rule == "effective_value":
        return contract.effective_value(**data)
    if rule == "effective_single_source_value":
        return contract.effective_single_source_value(**data)
    if rule == "is_actionable_lifecycle":
        return contract.is_actionable_lifecycle(data["value"])
    return getattr(contract, rule)(data["value"])


def test_compute_contract_fixture_replays_exactly() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert fixture["schema"] == "compute-conformance/v1"
    constants = dict(fixture["constants"])
    assert constants["LIFECYCLE_CHOICES"] == list(contract.COMPUTE_LIFECYCLE_CHOICES)
    assert constants["LINK_SOURCE_CHOICES"] == list(contract.SOURCE_CHOICES)
    for name, expected in constants.items():
        if name in {"LIFECYCLE_CHOICES", "LINK_SOURCE_CHOICES"}:
            continue
        actual = getattr(contract, name)
        assert (list(actual) if isinstance(actual, tuple) else actual) == expected
    for case in fixture["cases"]:
        try:
            actual = {"ok": _run(case)}
        except contract.ComputeContractError as exc:
            actual = {"error": {"code": exc.code, "path": exc.path, "str": str(exc)}}
        assert actual == case["result"], case["id"]
