import json

from typer.testing import CliRunner

import nctl_core.cli.main as main
from nctl_core.output import Envelope, EnvelopeError
from nctl_core.relations_render import RelationsConsumer, RelationsData, RelationsEdge, RelationsProvider

runner = CliRunner()


def _ok_envelope() -> Envelope[RelationsData]:
    data = RelationsData(
        generated_at="2026-07-15T12:00:00+00:00",
        edges=[
            RelationsEdge(
                consumer=RelationsConsumer(node="aghub", service="node-agent", placement_id="p1"),
                binding_name="llm_provider",
                provider=RelationsProvider(service="ollama", placement_id="p2", node="agstudio", endpoint="ollama-api", url="http://agstudio:11434/v1"),
                state="satisfied",
                gap_codes=[],
                evidence={"age_hours": 0.1},
            ),
        ],
        unreferenced=["orphan"],
        summary={"satisfied": 1},
    )
    return Envelope.build("nctl.relations.v1", data, [])


def _failed_envelope() -> Envelope[RelationsData]:
    return Envelope.build("nctl.relations.v1", RelationsData(), [EnvelopeError(code="nautobot_fetch_failed", message="boom")])


def test_relations_default_prints_text_to_stdout(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_relations", lambda cfg, host=None, service=None: _ok_envelope())

    result = runner.invoke(main.app, ["relations"])

    assert result.exit_code == 0
    assert "aghub/node-agent —llm_provider→ ollama @agstudio [satisfied, 0.1h]" in result.stdout
    assert "unreferenced (informational): orphan" in result.stdout
    assert "summary: satisfied=1" in result.stdout


def test_relations_json_prints_envelope(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_relations", lambda cfg, host=None, service=None: _ok_envelope())

    result = runner.invoke(main.app, ["relations", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema"] == "nctl.relations.v1"
    assert payload["data"]["unreferenced"] == ["orphan"]


def test_relations_passes_host_and_service_filters_through(monkeypatch):
    captured = {}

    def fake_build_relations(cfg, host=None, service=None):
        captured["host"] = host
        captured["service"] = service
        return _ok_envelope()

    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_relations", fake_build_relations)

    result = runner.invoke(main.app, ["relations", "--host", "aghub", "--service", "node-agent"])

    assert result.exit_code == 0
    assert captured == {"host": "aghub", "service": "node-agent"}


def test_relations_exit_1_on_failure(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_relations", lambda cfg, host=None, service=None: _failed_envelope())

    result = runner.invoke(main.app, ["relations"])

    assert result.exit_code == 1
    assert "error [nautobot_fetch_failed]" in result.stdout
