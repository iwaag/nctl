import json

from typer.testing import CliRunner

import nctl_core.cli.main as main
from nctl_core.nautobot import NautobotInfo
from nctl_core.output import Envelope
from nctl_core.status import DumpsStatus, StatusData

runner = CliRunner()


def _canned_envelope(ok: bool) -> Envelope[StatusData]:
    data = StatusData(
        operation_id="01JTESTULID000000000000000",
        nautobot=NautobotInfo(reachable=ok, url="http://nautobot.test", version="3.1.3", authenticated=ok, intent_catalog=ok),
        dumps=DumpsStatus(dir="/var/lib/nodeutils", hosts=[], errors=[]),
        submodules=[],
    )
    return Envelope.build("nctl.status.v1", data) if ok else _with_error(data)


def _with_error(data):
    from nctl_core.output import EnvelopeError

    return Envelope.build("nctl.status.v1", data, [EnvelopeError(code="nautobot_unreachable", message="boom")])


def test_status_json_exit_0_when_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_status", lambda cfg: _canned_envelope(ok=True))

    result = runner.invoke(main.app, ["status", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["data"]["nautobot"]["reachable"] is True


def test_status_json_exit_1_when_not_ok(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_status", lambda cfg: _with_error(_canned_envelope(ok=True).data))

    result = runner.invoke(main.app, ["status", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False


def test_status_text_mode_shows_checkmarks(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_status", lambda cfg: _canned_envelope(ok=True))

    result = runner.invoke(main.app, ["status"])
    assert result.exit_code == 0
    assert "✓ nautobot" in result.stdout
