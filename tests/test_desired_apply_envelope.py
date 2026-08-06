from types import SimpleNamespace

import pytest

import nctl_core.desired_apply as desired_apply


class _StubClient:
    def __init__(self, *_args, **_kwargs):
        pass

    def close(self) -> None:
        pass


@pytest.fixture()
def stub_transport(monkeypatch):
    calls = {}

    def fake_submit(client, operations, *, dry_run):
        calls["operations"] = operations
        calls["dry_run"] = dry_run
        return {"ok": True}

    monkeypatch.setattr(desired_apply, "NautobotClient", _StubClient)
    monkeypatch.setattr(desired_apply, "submit_batch", fake_submit)
    return calls


CFG = SimpleNamespace(nautobot=SimpleNamespace(url="http://x", resolve_token=lambda: "t"))

OPERATIONS_ONLY = """\
operations:
  - op: upsert
    kind: desired_endpoint
    key: {desired_node: agdnsmasq, name: primary, endpoint_type: primary}
    values: {gateway_address: 192.168.50.1}
"""


def test_dry_run_key_is_optional(tmp_path, stub_transport):
    document = tmp_path / "batch.yaml"
    document.write_text(OPERATIONS_ONLY, encoding="utf-8")

    result = desired_apply.apply_document(CFG, str(document), commit=False)

    assert result == {"ok": True}
    assert stub_transport["dry_run"] is True
    assert stub_transport["operations"][0]["kind"] == "desired_endpoint"


def test_explicit_dry_run_is_still_overridden_by_commit(tmp_path, stub_transport):
    document = tmp_path / "batch.yaml"
    document.write_text("dry_run: true\n" + OPERATIONS_ONLY, encoding="utf-8")

    desired_apply.apply_document(CFG, str(document), commit=True)

    assert stub_transport["dry_run"] is False


@pytest.mark.parametrize("text", [
    "just a string",
    "dry_run: true",
    "operations: []\nextra_key: 1",
])
def test_invalid_envelope_error_includes_minimal_template(tmp_path, stub_transport, text):
    document = tmp_path / "batch.yaml"
    document.write_text(text, encoding="utf-8")

    with pytest.raises(ValueError) as excinfo:
        desired_apply.apply_document(CFG, str(document), commit=False)

    message = str(excinfo.value)
    assert "Minimal partial-upsert template" in message
    assert "key: {desired_node: NODE_SLUG, name: primary, endpoint_type: primary}" in message
    assert not stub_transport
