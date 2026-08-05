import json
from datetime import datetime, timezone

from typer.testing import CliRunner

import nctl_core.cli.main as main
from nctl_core.config import ConfigInvalidError
from nctl_core.output import Envelope, EnvelopeError
from nctl_core.upload import UPLOAD_SCHEMA, UploadData

runner = CliRunner()


def _ok_envelope() -> Envelope[UploadData]:
    data = UploadData(
        url="http://agstudio.local:9100/nctl-outbox/2026-08-05/143012-abc123/state.json?signed",
        expires_at=datetime(2026, 8, 5, 15, 0, 12, tzinfo=timezone.utc),
        ttl_minutes=30,
        object_key="2026-08-05/143012-abc123/state.json",
        size_bytes=13,
        zipped=False,
        sources=["state.json"],
    )
    return Envelope.build(UPLOAD_SCHEMA, data, [])


def test_upload_passes_args_and_prints_url(monkeypatch, tmp_path):
    captured = {}

    def fake_build_upload(cfg, paths, *, zip_requested, ttl, store):
        captured.update(paths=paths, zip_requested=zip_requested, ttl=ttl, store=store)
        return _ok_envelope()

    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "make_store", lambda cfg: "the-store")
    monkeypatch.setattr(main, "build_upload", fake_build_upload)

    result = runner.invoke(main.app, ["upload", "state.json", "--zip", "--ttl", "2h"])

    assert result.exit_code == 0
    assert [p.name for p in captured["paths"]] == ["state.json"]
    assert captured["zip_requested"] is True
    assert captured["ttl"] == "2h"
    assert captured["store"] == "the-store"
    assert "?signed" in result.output
    assert "valid until" in result.output


def test_upload_json_emits_envelope(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "make_store", lambda cfg: object())
    monkeypatch.setattr(
        main, "build_upload", lambda cfg, paths, *, zip_requested, ttl, store: _ok_envelope()
    )

    result = runner.invoke(main.app, ["upload", "state.json", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["schema"] == UPLOAD_SCHEMA
    assert payload["data"]["object_key"] == "2026-08-05/143012-abc123/state.json"


def test_upload_without_storage_section_is_usage_error(monkeypatch):
    def raising_make_store(cfg):
        raise ConfigInvalidError("nctl upload requires a [storage] section in nctl.toml")

    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "make_store", raising_make_store)

    result = runner.invoke(main.app, ["upload", "state.json"])

    assert result.exit_code == 2
    assert "[storage]" in result.output


def test_upload_missing_path_is_usage_error(monkeypatch):
    envelope = Envelope.build(
        UPLOAD_SCHEMA,
        UploadData(),
        [EnvelopeError(code="missing_path", message="path does not exist: nope")],
    )
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "make_store", lambda cfg: object())
    monkeypatch.setattr(
        main, "build_upload", lambda cfg, paths, *, zip_requested, ttl, store: envelope
    )

    result = runner.invoke(main.app, ["upload", "nope"])

    assert result.exit_code == 2
    assert "missing_path" in result.output


def test_upload_store_failure_is_failure_exit(monkeypatch):
    envelope = Envelope.build(
        UPLOAD_SCHEMA,
        UploadData(),
        [EnvelopeError(code="upload_failed", message="S3Error: boom")],
    )
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "make_store", lambda cfg: object())
    monkeypatch.setattr(
        main, "build_upload", lambda cfg, paths, *, zip_requested, ttl, store: envelope
    )

    result = runner.invoke(main.app, ["upload", "state.json"])

    assert result.exit_code == 1
    assert "upload_failed" in result.output
