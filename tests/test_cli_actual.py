import json

from typer.testing import CliRunner

import nctl_core.cli.main as main
from nctl_core.actual_render import ActualClusterData, ActualData, ActualGuestData
from nctl_core.output import Envelope, EnvelopeError

runner = CliRunner()


def _canned_envelope(ok: bool) -> Envelope[ActualData]:
    data = ActualData(
        clusters=[
            ActualClusterData(
                id="cluster-1",
                name="aghub-proxmox",
                observation_state="complete",
                observed_at="2026-07-24T00:00:00+00:00",
                observer_device_id="aghub-device-uuid",
                guests=[
                    ActualGuestData(
                        id="vm-108", name="agdnsmasq", guest_type="lxc", vmid=108,
                        node="aghub", proxmox_status="running",
                    )
                ],
            )
        ]
    )
    if ok:
        return Envelope.build("nctl.actual.v1", data)
    return Envelope.build("nctl.actual.v1", data, [EnvelopeError(code="nautobot_unreachable", message="boom")])


def test_actual_json_exit_0_when_ok(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_actual", lambda cfg: _canned_envelope(ok=True))

    result = runner.invoke(main.app, ["actual", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["data"]["clusters"][0]["name"] == "aghub-proxmox"
    assert payload["data"]["clusters"][0]["guests"][0]["vmid"] == 108


def test_actual_json_exit_1_when_not_ok(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_actual", lambda cfg: _canned_envelope(ok=False))

    result = runner.invoke(main.app, ["actual", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False


def test_actual_text_mode_shows_aghub_proxmox_agdnsmasq_vmid_108(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_actual", lambda cfg: _canned_envelope(ok=True))

    result = runner.invoke(main.app, ["actual"])

    assert result.exit_code == 0
    assert "aghub-proxmox" in result.stdout
    assert "vmid=108" in result.stdout
    assert "agdnsmasq" in result.stdout


def test_actual_json_output_never_contains_raw_or_unrelated_data(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_actual", lambda cfg: _canned_envelope(ok=True))

    result = runner.invoke(main.app, ["actual", "--json"])

    assert "inventory_raw_json" not in result.stdout
    assert "aghub-pve" not in result.stdout
