from pathlib import Path

import pytest

from nctl_core.config import (
    CONFIG_ENV_VAR,
    Config,
    ConfigInvalidError,
    ConfigNotFoundError,
    find_config,
)

VALID = """
[nautobot]
url = "http://localhost:8000"

[inventory]
dumps_dir = "/var/lib/nodeutils"
"""


def write_config(directory: Path, body: str = VALID) -> Path:
    path = directory / "nctl.toml"
    path.write_text(body)
    return path


def make_repo_root(directory: Path) -> None:
    (directory / ".gitmodules").write_text('[submodule "nctl"]\n\tpath = nctl\n')


def test_explicit_path_wins(tmp_path, monkeypatch):
    explicit_dir = tmp_path / "a"
    explicit_dir.mkdir()
    explicit = write_config(explicit_dir)
    other_dir = tmp_path / "b"
    other_dir.mkdir()
    write_config(other_dir)
    monkeypatch.setenv(CONFIG_ENV_VAR, str(other_dir / "nctl.toml"))
    assert find_config(explicit, cwd=other_dir) == explicit


def test_env_var_beats_cwd(tmp_path, monkeypatch):
    env_dir = tmp_path / "env"
    env_dir.mkdir()
    env_config = write_config(env_dir)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    write_config(cwd)
    monkeypatch.setenv(CONFIG_ENV_VAR, str(env_config))
    assert find_config(cwd=cwd) == env_config


def test_cwd_beats_repo_root(tmp_path, monkeypatch):
    monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)
    make_repo_root(tmp_path)
    write_config(tmp_path)
    cwd = tmp_path / "sub"
    cwd.mkdir()
    local = write_config(cwd)
    assert find_config(cwd=cwd) == local


def test_repo_root_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)
    make_repo_root(tmp_path)
    root_config = write_config(tmp_path)
    cwd = tmp_path / "nctl" / "src"
    cwd.mkdir(parents=True)
    assert find_config(cwd=cwd) == root_config


def test_not_found(tmp_path, monkeypatch):
    monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)
    with pytest.raises(ConfigNotFoundError):
        find_config(cwd=tmp_path)


def test_load_valid(tmp_path):
    path = write_config(tmp_path)
    cfg = Config.load(path)
    assert cfg.nautobot.url == "http://localhost:8000"
    assert cfg.nautobot.token_env == "NAUTOBOT_TOKEN"
    assert cfg.inventory.dumps_dir == Path("/var/lib/nodeutils")
    assert cfg.repo_root() == tmp_path.resolve()


def test_load_rejects_inline_token(tmp_path):
    body = VALID.replace('url = "http://localhost:8000"', 'url = "x"\ntoken = "secret"')
    path = write_config(tmp_path, body)
    with pytest.raises(ConfigInvalidError, match="token"):
        Config.load(path)


def test_load_rejects_malformed_toml(tmp_path):
    path = write_config(tmp_path, "not toml [")
    with pytest.raises(ConfigInvalidError):
        Config.load(path)


def test_load_rejects_missing_section(tmp_path):
    path = write_config(tmp_path, "[nautobot]\nurl = 'x'\n")
    with pytest.raises(ConfigInvalidError):
        Config.load(path)


def test_resolve_token_from_env(tmp_path, monkeypatch):
    cfg = Config.load(write_config(tmp_path))
    monkeypatch.setenv("NAUTOBOT_TOKEN", "tok123")
    assert cfg.nautobot.resolve_token() == "tok123"


def test_resolve_token_from_file(tmp_path, monkeypatch):
    token_file = tmp_path / "token"
    token_file.write_text("tok456\n")
    cfg = Config.load(write_config(tmp_path))
    cfg = cfg.model_copy(update={"nautobot": cfg.nautobot.model_copy(update={"token_file": token_file})})
    monkeypatch.setenv("NAUTOBOT_TOKEN", "should-not-win")
    assert cfg.nautobot.resolve_token() == "tok456"
