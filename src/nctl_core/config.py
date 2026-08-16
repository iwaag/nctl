"""Configuration layer: locate, parse, and validate nctl.toml."""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

CONFIG_FILENAME = "nctl.toml"
CONFIG_ENV_VAR = "NCTL_CONFIG"


class ConfigError(Exception):
    """Raised when nctl.toml cannot be found, parsed, or validated."""


class ConfigNotFoundError(ConfigError):
    pass


class ConfigInvalidError(ConfigError):
    pass


class StrictModel(BaseModel):
    # extra="forbid" also rejects an inline `token` key: credentials must come
    # from token_env or token_file, never from nctl.toml itself.
    model_config = ConfigDict(extra="forbid")


class NautobotConfig(StrictModel):
    url: str
    token_env: str = "NAUTOBOT_TOKEN"
    token_file: Path | None = None

    def resolve_token(self) -> str | None:
        """Return the API token from token_file or the token_env variable, if set."""
        if self.token_file is not None:
            path = self.token_file.expanduser()
            if not path.is_file():
                raise ConfigInvalidError(f"nautobot.token_file does not exist: {path}")
            return path.read_text().strip()
        return os.environ.get(self.token_env)


class StorageConfig(StrictModel):
    """MinIO object storage for `nctl upload` presigned download URLs.

    `endpoint` is both the upload target and the host baked into presigned
    URLs (signatures cover the host), so one value owns both roles.
    """

    endpoint: str
    bucket: str
    access_key: str
    secret_key_env: str = "NCTL_STORAGE_SECRET"
    secret_key_file: Path | None = None
    default_ttl_minutes: int = Field(default=30, ge=1, le=10080)

    def resolve_secret_key(self, config_dir: Path) -> str | None:
        """Return the secret key from secret_key_file or the secret_key_env variable, if set.

        A relative secret_key_file resolves against the loaded nctl.toml's
        directory, like the [ssh] paths.
        """
        if self.secret_key_file is not None:
            path = resolve_local_path(self.secret_key_file, config_dir)
            if not path.is_file():
                raise ConfigInvalidError(f"storage.secret_key_file does not exist: {path}")
            return path.read_text().strip()
        return os.environ.get(self.secret_key_env)


class InventoryConfig(StrictModel):
    dumps_dir: Path = Path("~/.local/state/nctl/dumps")

    def resolved_dumps_dir(self) -> Path:
        return self.dumps_dir.expanduser()


class EventsConfig(StrictModel):
    log_dir: Path = Path("~/.local/state/nctl/events")

    def resolved_log_dir(self) -> Path:
        return self.log_dir.expanduser()


class AnsibleConfig(StrictModel):
    playbook_dir: Path
    inventory: Path

    def resolved_playbook_dir(self, config_dir: Path) -> Path:
        path = self.playbook_dir.expanduser()
        if not path.is_absolute():
            path = config_dir / path
        return path.resolve()

    def resolved_inventory(self, config_dir: Path) -> Path:
        path = self.inventory.expanduser()
        if not path.is_absolute():
            path = self.resolved_playbook_dir(config_dir) / path
        return path.resolve()


class RepoConfig(StrictModel):
    root: Path = Path(".")


class ReconcileConfig(StrictModel):
    max_rounds: int = Field(default=3, ge=1, le=10)
    job_poll_interval_seconds: float = Field(default=2.0, gt=0, le=60)
    job_timeout_seconds: float = Field(default=300.0, gt=0, le=86400)
    ansible_timeout_seconds: float = Field(default=1800.0, gt=0, le=86400)
    remote_report_path: Path = Path("/var/lib/nodeutils/inventory.json")
    max_report_bytes: int = Field(default=2_097_152, ge=1, le=100_000_000)
    max_report_age_hours: int = Field(default=72, gt=0, le=8760)
    ingest_policy_file: Path = Path("seed/nodeutils_ingest.yaml")
    service_observation_max_age_hours: int = Field(default=24, gt=0, le=8760)
    workspace_observation_max_age_hours: int = Field(default=24, gt=0, le=8760)
    lock_path: Path = Path("~/.local/state/nctl/reconcile.lock")
    # Normally resolved from the superproject's nodeutils gitlink. Packaged
    # controllers without superproject metadata may pin the same full SHA here.
    nodeutils_version: str | None = None

    @field_validator("remote_report_path")
    @classmethod
    def remote_report_path_must_be_absolute(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("remote_report_path must be absolute")
        return value

    @field_validator("nodeutils_version")
    @classmethod
    def nodeutils_version_must_be_full_git_object_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.lower()
        if re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", normalized) is None:
            raise ValueError("nodeutils_version must be a full 40- or 64-character Git object ID")
        return normalized

    def resolved_lock_path(self) -> Path:
        return self.lock_path.expanduser()


def resolve_local_path(path: Path, config_dir: Path) -> Path:
    """Canonicalize a config-relative local path (fix_sshkey2 plan.md Step 1 / Corrected contract 2).

    Expands `~`, keeps an already-absolute path absolute, and resolves a
    relative path against `config_dir` (the loaded `nctl.toml`'s parent
    directory) rather than the process working directory. This is the single
    authoritative resolver for local SSH trust-store paths so enrollment,
    inventory rendering, preflight, and dnsmasq apply always agree on one file
    regardless of cwd.
    """
    expanded = path.expanduser()
    if expanded.is_absolute():
        return expanded
    return (config_dir / expanded).resolve()


class SshConfig(StrictModel):
    # Local controller trust state, not a generated repo artifact or nintent
    # desired/actual state: see devdocs/small/fix_sshkey/plan.md Design Decision 2.
    known_hosts_file: Path = Path("~/.local/state/nctl/ssh/known_hosts")
    keyscan_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    lock_path: Path = Path("~/.local/state/nctl/ssh.lock")

    def resolved_known_hosts_file(self, config_dir: Path) -> Path:
        return resolve_local_path(self.known_hosts_file, config_dir)

    def resolved_lock_path(self, config_dir: Path) -> Path:
        return resolve_local_path(self.lock_path, config_dir)


def _read_env_file(path: Path, *, setting: str) -> dict[str, str]:
    """Read a `KEY=value` credentials file into a mapping, values never logged."""
    if not path.is_file():
        raise ConfigInvalidError(f"{setting} does not exist: {path}")
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


class ZulipConfig(StrictModel):
    """Realm URL plus an existing 0600 bot env file (agent_intent p1 step 3).

    `credentials_file` is the same `ZULIP_URL/ZULIP_EMAIL/ZULIP_API_KEY` shape the
    listeners already use, so the collector reuses one credential store rather than
    copying a key into a second place. `verify_tls` exists because this realm is
    served with a self-signed certificate on the LAN.
    """

    url: str
    credentials_file: Path
    verify_tls: bool = True

    def resolve_credentials(self, config_dir: Path) -> tuple[str, str]:
        values = _read_env_file(
            resolve_local_path(self.credentials_file, config_dir),
            setting="zulip.credentials_file",
        )
        missing = sorted({"ZULIP_EMAIL", "ZULIP_API_KEY"} - set(values))
        if missing:
            raise ConfigInvalidError(
                f"zulip.credentials_file is missing {', '.join(missing)}"
            )
        return values["ZULIP_EMAIL"], values["ZULIP_API_KEY"]


class PlaneConfig(StrictModel):
    """Plane CE workspace plus an existing 0600 env file holding `PLANE_API_KEY`."""

    url: str
    workspace_slug: str
    credentials_file: Path

    def resolve_api_key(self, config_dir: Path) -> str:
        values = _read_env_file(
            resolve_local_path(self.credentials_file, config_dir),
            setting="plane.credentials_file",
        )
        if "PLANE_API_KEY" not in values:
            raise ConfigInvalidError("plane.credentials_file is missing PLANE_API_KEY")
        return values["PLANE_API_KEY"]


class Config(StrictModel):
    nautobot: NautobotConfig
    inventory: InventoryConfig
    events: EventsConfig = EventsConfig()
    ansible: AnsibleConfig
    repo: RepoConfig = RepoConfig()
    reconcile: ReconcileConfig = ReconcileConfig()
    ssh: SshConfig = SshConfig()
    storage: StorageConfig | None = None
    zulip: ZulipConfig | None = None
    plane: PlaneConfig | None = None

    # Where the config file was loaded from; relative paths resolve against its parent.
    source_path: Path

    def repo_root(self) -> Path:
        root = self.repo.root.expanduser()
        if not root.is_absolute():
            root = (self.source_path.parent / root).resolve()
        return root

    def resolved_ssh_known_hosts_file(self) -> Path:
        return self.ssh.resolved_known_hosts_file(self.source_path.parent)

    def resolved_ssh_lock_path(self) -> Path:
        return self.ssh.resolved_lock_path(self.source_path.parent)

    def require_storage(self) -> StorageConfig:
        if self.storage is None:
            raise ConfigInvalidError(
                "nctl upload requires a [storage] section in nctl.toml "
                "(endpoint, bucket, access_key, secret_key_file or secret_key_env)"
            )
        return self.storage

    def require_zulip(self) -> ZulipConfig:
        if self.zulip is None:
            raise ConfigInvalidError(
                "observing agent registration requires a [zulip] section in nctl.toml "
                "(url, credentials_file)"
            )
        return self.zulip

    def require_plane(self) -> PlaneConfig:
        if self.plane is None:
            raise ConfigInvalidError(
                "observing agent registration requires a [plane] section in nctl.toml "
                "(url, workspace_slug, credentials_file)"
            )
        return self.plane

    def resolved_storage_secret_key(self) -> str | None:
        return self.require_storage().resolve_secret_key(self.source_path.parent)

    @classmethod
    def load(cls, explicit_path: Path | None = None, cwd: Path | None = None) -> "Config":
        path = find_config(explicit_path, cwd=cwd).resolve()
        try:
            raw = tomllib.loads(path.read_text())
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigInvalidError(f"cannot parse {path}: {exc}") from exc
        try:
            return cls.model_validate({**raw, "source_path": path})
        except ValidationError as exc:
            raise ConfigInvalidError(f"invalid config {path}: {exc}") from exc


def find_config(explicit_path: Path | None = None, cwd: Path | None = None) -> Path:
    """Resolve the config file location.

    Order: explicit --config path > $NCTL_CONFIG > ./nctl.toml > nctl.toml at the
    pj-clusterintent repo root (nearest ancestor whose .gitmodules mentions nctl).
    """
    if explicit_path is not None:
        if not explicit_path.is_file():
            raise ConfigNotFoundError(f"config file not found: {explicit_path}")
        return explicit_path

    env_path = os.environ.get(CONFIG_ENV_VAR)
    if env_path:
        path = Path(env_path).expanduser()
        if not path.is_file():
            raise ConfigNotFoundError(f"${CONFIG_ENV_VAR} points to a missing file: {path}")
        return path

    cwd = (cwd or Path.cwd()).resolve()
    local = cwd / CONFIG_FILENAME
    if local.is_file():
        return local

    root = find_repo_root(cwd)
    if root is not None:
        candidate = root / CONFIG_FILENAME
        if candidate.is_file():
            return candidate

    raise ConfigNotFoundError(
        f"no {CONFIG_FILENAME} found (searched --config, ${CONFIG_ENV_VAR}, {cwd}, and the repo root)"
    )


def find_repo_root(start: Path) -> Path | None:
    """Walk up from `start` to the nearest directory whose .gitmodules registers nctl."""
    for directory in [start, *start.parents]:
        gitmodules = directory / ".gitmodules"
        if gitmodules.is_file() and "nctl" in gitmodules.read_text():
            return directory
    return None
