"""Configuration layer: locate, parse, and validate nctl.toml."""

from __future__ import annotations

import os
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


class DashboardConfig(StrictModel):
    out_dir: Path = Path("~/.local/state/nctl/dashboard")
    # Where the out_dir is served on the LAN, if anywhere. Informational only:
    # nctl never fetches it; it is surfaced in output and pushed into docs.
    url: str | None = None

    def resolved_out_dir(self) -> Path:
        return self.out_dir.expanduser()


class ReconcileConfig(StrictModel):
    max_rounds: int = Field(default=3, ge=1, le=10)
    job_poll_interval_seconds: float = Field(default=2.0, gt=0, le=60)
    job_timeout_seconds: float = Field(default=300.0, gt=0, le=86400)
    ansible_timeout_seconds: float = Field(default=1800.0, gt=0, le=86400)
    remote_report_path: Path = Path("/var/lib/nodeutils/inventory.json")
    lock_path: Path = Path("~/.local/state/nctl/reconcile.lock")

    @field_validator("remote_report_path")
    @classmethod
    def remote_report_path_must_be_absolute(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("remote_report_path must be absolute")
        return value

    def resolved_lock_path(self) -> Path:
        return self.lock_path.expanduser()


class Config(StrictModel):
    nautobot: NautobotConfig
    inventory: InventoryConfig
    events: EventsConfig = EventsConfig()
    ansible: AnsibleConfig
    repo: RepoConfig = RepoConfig()
    dashboard: DashboardConfig = DashboardConfig()
    reconcile: ReconcileConfig = ReconcileConfig()

    # Where the config file was loaded from; relative paths resolve against its parent.
    source_path: Path

    def repo_root(self) -> Path:
        root = self.repo.root.expanduser()
        if not root.is_absolute():
            root = (self.source_path.parent / root).resolve()
        return root

    @classmethod
    def load(cls, explicit_path: Path | None = None, cwd: Path | None = None) -> "Config":
        path = find_config(explicit_path, cwd=cwd)
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
