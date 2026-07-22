import json

from typer.testing import CliRunner

import nctl_core.cli.main as main
from nctl_core.dnsmasq_apply import DnsmasqApplyData
from nctl_core.output import Envelope, EnvelopeError

runner = CliRunner()


def _envelope(ok=True):
    data = DnsmasqApplyData(
        operation_id="01JTESTULID000000000000000",
        mode="dry-run",
        artifact_path="/tmp/artifact.conf",
        event_log_path="/tmp/events.jsonl",
        inventory_path="/tmp/inventory.yml",
        target_hosts=["agdnsmasq"],
    )
    errors = [] if ok else [EnvelopeError(code="ansible_dry_run_failed", message="boom")]
    return Envelope.build("nctl.apply.dnsmasq.v2", data, errors)


def test_apply_dnsmasq_defaults_to_dry_run(monkeypatch):
    seen = []
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_dnsmasq_apply", lambda cfg, apply_changes=False, inventory=None: seen.append(apply_changes) or _envelope())

    result = runner.invoke(main.app, ["apply", "dnsmasq"])

    assert result.exit_code == 0
    assert seen == [False]
    assert "operation_id:" in result.stdout


def test_apply_dnsmasq_yes_requests_real_apply(monkeypatch):
    seen = []
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_dnsmasq_apply", lambda cfg, apply_changes=False, inventory=None: seen.append(apply_changes) or _envelope())

    result = runner.invoke(main.app, ["apply", "dnsmasq", "--yes"])

    assert result.exit_code == 0
    assert seen == [True]


def test_apply_dnsmasq_json_is_one_envelope(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_dnsmasq_apply", lambda cfg, apply_changes=False, inventory=None: _envelope())

    result = runner.invoke(main.app, ["apply", "dnsmasq", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema"] == "nctl.apply.dnsmasq.v2"
    assert payload["data"]["operation_id"] == "01JTESTULID000000000000000"


def test_apply_dnsmasq_failure_exits_one(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_dnsmasq_apply", lambda cfg, apply_changes=False, inventory=None: _envelope(ok=False))

    result = runner.invoke(main.app, ["apply", "dnsmasq", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout)["errors"][0]["code"] == "ansible_dry_run_failed"


def test_apply_dnsmasq_inventory_option_is_passed_through(monkeypatch, tmp_path):
    seen = []
    inventory_path = tmp_path / "hosts_intent.yml"
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(
        main,
        "build_dnsmasq_apply",
        lambda cfg, apply_changes=False, inventory=None: seen.append(inventory) or _envelope(),
    )

    result = runner.invoke(main.app, ["apply", "dnsmasq", "--inventory", str(inventory_path)])

    assert result.exit_code == 0
    assert seen == [inventory_path]


def test_apply_dnsmasq_without_inventory_option_passes_none(monkeypatch):
    seen = []
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(
        main,
        "build_dnsmasq_apply",
        lambda cfg, apply_changes=False, inventory=None: seen.append(inventory) or _envelope(),
    )

    result = runner.invoke(main.app, ["apply", "dnsmasq"])

    assert result.exit_code == 0
    assert seen == [None]
