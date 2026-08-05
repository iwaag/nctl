import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nctl_core.config import Config, ConfigInvalidError
from nctl_core.upload import (
    UPLOAD_SCHEMA,
    UploadError,
    build_upload,
    make_store,
    parse_ttl_minutes,
    render_upload_text,
    run_upload,
)

CONFIG_BODY = """
[nautobot]
url = "http://localhost:8000"

[inventory]
dumps_dir = "/var/lib/nodeutils"

[ansible]
playbook_dir = "ansible_agdev"
inventory = "inventories/generated/hosts_intent.yml"

[storage]
endpoint = "http://agstudio.local:9100"
bucket = "nctl-outbox"
access_key = "nctl"
default_ttl_minutes = 30
"""

NOW = datetime(2026, 8, 5, 14, 30, 12, tzinfo=timezone.utc)


class FakeStore:
    """Records uploads; keeps the uploaded bytes so zip contents can be asserted."""

    def __init__(self):
        self.puts = []
        self.presigns = []

    def put_file(self, key: str, path: Path) -> None:
        self.puts.append((key, path.name, path.read_bytes()))

    def presign_get(self, key: str, ttl: timedelta) -> str:
        self.presigns.append((key, ttl))
        return f"http://agstudio.local:9100/nctl-outbox/{key}?signed"


@pytest.fixture
def cfg(tmp_path) -> Config:
    path = tmp_path / "nctl.toml"
    path.write_text(CONFIG_BODY)
    return Config.load(path)


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


# --- Tier B: ttl parsing ---------------------------------------------------

@pytest.mark.parametrize(
    "raw,minutes",
    [("30", 30), ("30m", 30), ("2h", 120), ("1", 1), ("10080", 10080), (" 45m ", 45)],
)
def test_parse_ttl_minutes_accepts(raw, minutes):
    assert parse_ttl_minutes(raw) == minutes


@pytest.mark.parametrize("raw", ["", "0", "10081", "169h", "-5", "5d", "abc", "1.5h"])
def test_parse_ttl_minutes_rejects(raw):
    with pytest.raises(UploadError) as exc:
        parse_ttl_minutes(raw)
    assert exc.value.code == "invalid_ttl"


# --- single file, no zip ---------------------------------------------------

def test_single_file_uploads_as_is(cfg, store, tmp_path):
    source = tmp_path / "state.json"
    source.write_text('{"drift": []}')

    data = run_upload(cfg, [source], store=store, now=NOW)

    assert not data.zipped
    (key, name, body) = store.puts[0]
    assert name == "state.json"
    assert body == b'{"drift": []}'
    assert key == data.object_key
    assert key.startswith("2026-08-05/143012-")
    assert key.endswith("/state.json")
    assert data.size_bytes == source.stat().st_size
    assert data.url.endswith("?signed")
    assert data.ttl_minutes == 30  # storage default
    assert data.expires_at == NOW + timedelta(minutes=30)
    assert store.presigns == [(key, timedelta(minutes=30))]


def test_repeated_uploads_never_collide(cfg, store, tmp_path):
    source = tmp_path / "state.json"
    source.write_text("x")
    first = run_upload(cfg, [source], store=store, now=NOW)
    second = run_upload(cfg, [source], store=store, now=NOW)
    assert first.object_key != second.object_key


def test_ttl_override_beats_default(cfg, store, tmp_path):
    source = tmp_path / "state.json"
    source.write_text("x")
    data = run_upload(cfg, [source], store=store, now=NOW, ttl="2h")
    assert data.ttl_minutes == 120
    assert data.expires_at == NOW + timedelta(hours=2)
    assert store.presigns[0][1] == timedelta(hours=2)


# --- zip paths -------------------------------------------------------------

def _zip_names(body: bytes, tmp_path: Path) -> list[str]:
    blob = tmp_path / "roundtrip.zip"
    blob.write_bytes(body)
    with zipfile.ZipFile(blob) as archive:
        return sorted(archive.namelist())


def test_explicit_zip_of_single_file(cfg, store, tmp_path):
    source = tmp_path / "state.json"
    source.write_text("x")
    data = run_upload(cfg, [source], store=store, now=NOW, zip_requested=True)
    assert data.zipped
    key, name, body = store.puts[0]
    assert name == "state.zip"
    assert key.endswith("/state.zip")
    assert _zip_names(body, tmp_path) == ["state.json"]


def test_multiple_paths_bundle_into_one_zip(cfg, store, tmp_path):
    a = tmp_path / "a.txt"
    a.write_text("a")
    b = tmp_path / "b.txt"
    b.write_text("b")
    data = run_upload(cfg, [a, b], store=store, now=NOW)
    assert data.zipped
    key, name, body = store.puts[0]
    assert name == "bundle.zip"
    assert _zip_names(body, tmp_path) == ["a.txt", "b.txt"]
    assert len(store.puts) == 1  # exactly one object, one URL
    assert data.sources == [str(a), str(b)]


def test_directory_zips_recursively_with_dir_prefix(cfg, store, tmp_path):
    root = tmp_path / "evidence"
    (root / "sub").mkdir(parents=True)
    (root / "top.txt").write_text("t")
    (root / "sub" / "leaf.txt").write_text("l")
    data = run_upload(cfg, [root], store=store, now=NOW)
    assert data.zipped
    key, name, body = store.puts[0]
    assert name == "evidence.zip"
    assert _zip_names(body, tmp_path) == ["evidence/sub/leaf.txt", "evidence/top.txt"]


# --- error paths -----------------------------------------------------------

def test_missing_path_is_reported_before_any_upload(cfg, store, tmp_path):
    envelope = build_upload(cfg, [tmp_path / "absent.json"], store=store, now=NOW)
    assert not envelope.ok
    assert envelope.errors[0].code == "missing_path"
    assert str(tmp_path / "absent.json") in envelope.errors[0].message
    assert store.puts == []


def test_store_failure_becomes_upload_failed_error(cfg, tmp_path):
    class ExplodingStore:
        def put_file(self, key, path):
            raise RuntimeError("connection refused")

        def presign_get(self, key, ttl):
            raise AssertionError("must not presign after failed put")

    source = tmp_path / "state.json"
    source.write_text("x")
    envelope = build_upload(cfg, [source], store=ExplodingStore(), now=NOW)
    assert not envelope.ok
    assert envelope.errors[0].code == "upload_failed"
    assert "connection refused" in envelope.errors[0].message


def test_make_store_without_storage_section_raises_naming_section(tmp_path):
    body = CONFIG_BODY.split("[storage]")[0]
    path = tmp_path / "nctl.toml"
    path.write_text(body)
    cfg = Config.load(path)
    with pytest.raises(ConfigInvalidError, match=r"\[storage\]"):
        make_store(cfg)


def test_make_store_without_secret_raises_naming_both_sources(cfg, monkeypatch):
    monkeypatch.delenv("NCTL_STORAGE_SECRET", raising=False)
    with pytest.raises(ConfigInvalidError, match="secret_key_file or the NCTL_STORAGE_SECRET"):
        make_store(cfg)


# --- rendering and envelope ------------------------------------------------

def test_build_upload_envelope_and_text(cfg, store, tmp_path):
    source = tmp_path / "state.json"
    source.write_text("x")
    envelope = build_upload(cfg, [source], store=store, now=NOW)
    assert envelope.ok
    assert envelope.schema_name == UPLOAD_SCHEMA
    text = render_upload_text(envelope)
    assert envelope.data.url in text
    assert "valid until" in text
    assert envelope.data.object_key in text


def test_render_error_text(cfg, store, tmp_path):
    envelope = build_upload(cfg, [tmp_path / "absent"], store=store, now=NOW)
    text = render_upload_text(envelope)
    assert text.startswith("error [missing_path]")
