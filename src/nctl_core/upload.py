"""`nctl upload`: put files in the configured MinIO bucket, mint a presigned URL.

One invocation always yields exactly one download URL: a single regular file
uploads as-is; multiple paths, any directory, or an explicit --zip request
bundle everything into one zip first. The `[storage]` endpoint is both the
upload target and the host signed into the URL, so the two can never diverge.
"""

from __future__ import annotations

import re
import secrets
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from nctl_core.config import Config, ConfigInvalidError, StorageConfig
from nctl_core.output import Envelope, EnvelopeError

UPLOAD_SCHEMA = "nctl.upload.v1"

# --ttl accepts plain integer minutes or an integer with an m/h suffix.
_TTL_PATTERN = re.compile(r"^(\d+)([mh]?)$")
TTL_MIN_MINUTES = 1
TTL_MAX_MINUTES = 10080


class UploadError(Exception):
    def __init__(self, code: str, message: str, detail: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail or {}


def parse_ttl_minutes(raw: str) -> int:
    match = _TTL_PATTERN.match(raw.strip())
    if match is None:
        raise UploadError("invalid_ttl", f"invalid --ttl {raw!r}: use integer minutes, or e.g. 30m / 2h")
    value = int(match.group(1)) * (60 if match.group(2) == "h" else 1)
    if not TTL_MIN_MINUTES <= value <= TTL_MAX_MINUTES:
        raise UploadError(
            "invalid_ttl",
            f"--ttl {raw!r} is out of bounds ({TTL_MIN_MINUTES}..{TTL_MAX_MINUTES} minutes)",
        )
    return value


class ObjectStore(Protocol):
    """Seam for tests: the two MinIO operations upload needs."""

    def put_file(self, key: str, path: Path) -> None: ...

    def presign_get(self, key: str, ttl: timedelta) -> str: ...


class MinioStore:
    """Real ObjectStore over the minio SDK, configured from [storage]."""

    def __init__(self, storage: StorageConfig, secret_key: str) -> None:
        from minio import Minio  # imported here so tests never need the SDK

        parsed = urlparse(storage.endpoint)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ConfigInvalidError(
                f"storage.endpoint must be an http(s) URL with a host, got {storage.endpoint!r}"
            )
        self._bucket = storage.bucket
        self._client = Minio(
            parsed.netloc,
            access_key=storage.access_key,
            secret_key=secret_key,
            secure=parsed.scheme == "https",
        )

    def put_file(self, key: str, path: Path) -> None:
        self._client.fput_object(self._bucket, key, str(path))

    def presign_get(self, key: str, ttl: timedelta) -> str:
        return self._client.presigned_get_object(self._bucket, key, expires=ttl)


def make_store(cfg: Config) -> MinioStore:
    """Resolve [storage] plus its secret into a live MinIO client.

    Raises ConfigError when the section or the secret is missing, so the CLI
    can report a usage error naming what to configure.
    """
    storage = cfg.require_storage()
    secret = cfg.resolved_storage_secret_key()
    if secret is None:
        raise ConfigInvalidError(
            "no storage secret key: set storage.secret_key_file or the "
            f"{storage.secret_key_env} environment variable"
        )
    return MinioStore(storage, secret)


class UploadData(BaseModel):
    url: str = ""
    expires_at: datetime | None = None
    ttl_minutes: int = 0
    object_key: str = ""
    size_bytes: int = 0
    zipped: bool = False
    sources: list[str] = Field(default_factory=list)


def _object_name(paths: list[Path], zipped: bool) -> str:
    if not zipped:
        return paths[0].name
    if len(paths) == 1:
        base = paths[0].name if paths[0].is_dir() else paths[0].stem
        return f"{base}.zip"
    return "bundle.zip"


def _build_zip(paths: list[Path], destination: Path) -> None:
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            if path.is_dir():
                members = sorted(p for p in path.rglob("*") if p.is_file())
                for member in members:
                    archive.write(member, arcname=str(Path(path.name) / member.relative_to(path)))
            else:
                archive.write(path, arcname=path.name)


def run_upload(
    cfg: Config,
    paths: list[Path],
    *,
    zip_requested: bool = False,
    ttl: str | None = None,
    store: ObjectStore | None = None,
    now: datetime | None = None,
) -> UploadData:
    """Upload paths (zipping when needed) and return the presigned-URL result.

    Raises UploadError for bad input and lets store/config errors propagate.
    """
    storage = cfg.require_storage()
    ttl_minutes = parse_ttl_minutes(ttl) if ttl is not None else storage.default_ttl_minutes

    resolved = [path.expanduser() for path in paths]
    missing = [str(path) for path in resolved if not path.exists()]
    if missing:
        raise UploadError("missing_path", f"path does not exist: {', '.join(missing)}", {"paths": missing})

    zipped = zip_requested or len(resolved) > 1 or any(path.is_dir() for path in resolved)
    moment = now or datetime.now(timezone.utc)
    prefix = f"{moment:%Y-%m-%d}/{moment:%H%M%S}-{secrets.token_hex(3)}"
    object_key = f"{prefix}/{_object_name(resolved, zipped)}"

    if store is None:
        store = make_store(cfg)

    if zipped:
        with tempfile.TemporaryDirectory(prefix="nctl-upload-") as tmp:
            archive = Path(tmp) / _object_name(resolved, zipped)
            _build_zip(resolved, archive)
            size_bytes = archive.stat().st_size
            store.put_file(object_key, archive)
    else:
        size_bytes = resolved[0].stat().st_size
        store.put_file(object_key, resolved[0])

    ttl_delta = timedelta(minutes=ttl_minutes)
    url = store.presign_get(object_key, ttl_delta)
    return UploadData(
        url=url,
        expires_at=moment + ttl_delta,
        ttl_minutes=ttl_minutes,
        object_key=object_key,
        size_bytes=size_bytes,
        zipped=zipped,
        sources=[str(path) for path in resolved],
    )


def build_upload(
    cfg: Config,
    paths: list[Path],
    *,
    zip_requested: bool = False,
    ttl: str | None = None,
    store: ObjectStore | None = None,
    now: datetime | None = None,
) -> Envelope[UploadData]:
    try:
        data = run_upload(cfg, paths, zip_requested=zip_requested, ttl=ttl, store=store, now=now)
        return Envelope.build(UPLOAD_SCHEMA, data)
    except UploadError as exc:
        error = EnvelopeError(code=exc.code, message=str(exc), detail=exc.detail)
        return Envelope.build(UPLOAD_SCHEMA, UploadData(), errors=[error])
    except Exception as exc:  # S3/connection failures: report, do not traceback
        error = EnvelopeError(code="upload_failed", message=f"{type(exc).__name__}: {exc}")
        return Envelope.build(UPLOAD_SCHEMA, UploadData(), errors=[error])


def render_upload_text(envelope: Envelope[UploadData]) -> str:
    if not envelope.ok:
        return "\n".join(f"error [{error.code}]: {error.message}" for error in envelope.errors)
    data = envelope.data
    what = f"zip of {len(data.sources)} path(s)" if data.zipped else data.sources[0]
    expires = data.expires_at.strftime("%Y-%m-%d %H:%M:%S %Z") if data.expires_at else "?"
    return "\n".join(
        [
            f"uploaded {what} ({data.size_bytes} bytes)",
            f"object: {data.object_key}",
            f"download URL (valid until {expires}, {data.ttl_minutes} min):",
            data.url,
        ]
    )
