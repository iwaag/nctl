import json

from pydantic import BaseModel

from nctl_core.output import Envelope, EnvelopeError, emit


class Payload(BaseModel):
    value: int


def test_build_ok_when_no_errors():
    envelope = Envelope.build("nctl.test.v1", Payload(value=1))
    assert envelope.ok is True
    assert envelope.errors == []


def test_build_not_ok_when_errors():
    err = EnvelopeError(code="boom", message="it broke")
    envelope = Envelope.build("nctl.test.v1", Payload(value=1), [err])
    assert envelope.ok is False
    assert envelope.errors == [err]


def test_to_json_uses_schema_alias():
    envelope = Envelope.build("nctl.test.v1", Payload(value=1))
    parsed = json.loads(envelope.to_json())
    assert parsed["schema"] == "nctl.test.v1"
    assert "schema_name" not in parsed
    assert parsed["data"] == {"value": 1}
    assert parsed["ok"] is True
    assert parsed["errors"] == []


def test_emit_json_mode_prints_envelope_json(capsys):
    envelope = Envelope.build("nctl.test.v1", Payload(value=2))
    emit(envelope, True, lambda e: "should not be used")
    out = capsys.readouterr().out
    assert json.loads(out)["data"]["value"] == 2


def test_emit_text_mode_uses_render_text(capsys):
    envelope = Envelope.build("nctl.test.v1", Payload(value=3))
    emit(envelope, False, lambda e: f"value is {e.data.value}")
    out = capsys.readouterr().out
    assert out.strip() == "value is 3"
