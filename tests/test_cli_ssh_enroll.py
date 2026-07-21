import json

from typer.testing import CliRunner

import nctl_core.cli.main as main
from nctl_core.output import Envelope, EnvelopeError
from nctl_core.ssh_enroll import SSH_ENROLL_SCHEMA, SshEnrollData

runner = CliRunner()


def _ok_envelope(action: str = "enroll", applied: bool = False) -> Envelope[SshEnrollData]:
    data = SshEnrollData(
        operation_id="01OPID",
        mode="plan",
        action=action,
        applied=applied,
        node_id="27818c12-fe15-4c9f-83d0-7949523f6c33",
        node_slug="agdnsmasq",
        endpoint="agdnsmasq.local",
        port=22,
        alias="nctl-node-27818c12-fe15-4c9f-83d0-7949523f6c33",
        lookup_name="nctl-node-27818c12-fe15-4c9f-83d0-7949523f6c33",
        known_hosts_file="/fake/known_hosts",
    )
    return Envelope.build(SSH_ENROLL_SCHEMA, data)


def _failed_envelope(code: str) -> Envelope[SshEnrollData]:
    return Envelope.build(SSH_ENROLL_SCHEMA, SshEnrollData(), [EnvelopeError(code=code, message="boom")])


def test_ssh_enroll_default_prints_text(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_ssh_enroll", lambda cfg, host, **kwargs: _ok_envelope())

    result = runner.invoke(main.app, ["ssh", "enroll", "agdnsmasq"])

    assert result.exit_code == 0
    assert "action=enroll" in result.stdout


def test_ssh_enroll_json_prints_envelope(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_ssh_enroll", lambda cfg, host, **kwargs: _ok_envelope())

    result = runner.invoke(main.app, ["ssh", "enroll", "agdnsmasq", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema"] == SSH_ENROLL_SCHEMA


def test_ssh_enroll_passes_flags_through(monkeypatch):
    captured = {}

    def fake_build(cfg, host, **kwargs):
        captured["host"] = host
        captured.update(kwargs)
        return _ok_envelope()

    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_ssh_enroll", fake_build)

    result = runner.invoke(
        main.app,
        [
            "ssh",
            "enroll",
            "agdnsmasq",
            "--from-known-hosts",
            "--fingerprint",
            "SHA256:aaa",
            "--fingerprint",
            "SHA256:bbb",
            "--replace",
            "--yes",
        ],
    )

    assert result.exit_code == 0
    assert captured["host"] == "agdnsmasq"
    assert captured["from_known_hosts"] is True
    assert captured["fingerprints"] == ["SHA256:aaa", "SHA256:bbb"]
    assert captured["replace"] is True
    assert captured["apply_changes"] is True


def test_ssh_enroll_unknown_host_is_usage_error(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_ssh_enroll", lambda cfg, host, **kwargs: _failed_envelope("unknown_host"))

    result = runner.invoke(main.app, ["ssh", "enroll", "does-not-exist"])

    assert result.exit_code == 2


def test_ssh_enroll_host_key_unverified_is_failure_exit(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_ssh_enroll", lambda cfg, host, **kwargs: _failed_envelope("host_key_unverified"))

    result = runner.invoke(main.app, ["ssh", "enroll", "agdnsmasq"])

    assert result.exit_code == 1
