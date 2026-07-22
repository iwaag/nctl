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

[ansible]
playbook_dir = "ansible_agdev"
inventory = "inventories/generated/hosts_intent.yml"
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
    assert cfg.ansible.resolved_playbook_dir(tmp_path) == (tmp_path / "ansible_agdev").resolve()
    assert cfg.ansible.resolved_inventory(tmp_path) == (
        tmp_path / "ansible_agdev/inventories/generated/hosts_intent.yml"
    ).resolve()
    assert cfg.repo_root() == tmp_path.resolve()
    assert cfg.reconcile.max_rounds == 3
    assert cfg.reconcile.remote_report_path == Path("/var/lib/nodeutils/inventory.json")
    assert cfg.reconcile.max_report_bytes == 2_097_152
    assert cfg.reconcile.max_report_age_hours == 72
    assert cfg.reconcile.ingest_policy_file == Path("seed/nodeutils_ingest.yaml")
    assert cfg.reconcile.service_observation_max_age_hours == 24
    assert cfg.reconcile.resolved_lock_path().is_absolute()
    assert cfg.serve.host == "127.0.0.1"
    assert cfg.serve.port == 8300
    assert cfg.serve.auth == "token"
    assert cfg.serve.token_env == "NCTL_SERVE_TOKEN"
    assert cfg.serve.cors_origins == []


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


def test_load_rejects_unknown_ansible_key(tmp_path):
    path = write_config(tmp_path, VALID.replace('playbook_dir = "ansible_agdev"', 'playbook_dir = "ansible_agdev"\nunknown = true'))
    with pytest.raises(ConfigInvalidError, match="unknown"):
        Config.load(path)


def test_absolute_ansible_inventory_is_not_rebased(tmp_path):
    absolute = tmp_path / "inventory.yml"
    body = VALID.replace('inventory = "inventories/generated/hosts_intent.yml"', f'inventory = "{absolute}"')
    cfg = Config.load(write_config(tmp_path, body))
    assert cfg.ansible.resolved_inventory(tmp_path) == absolute


def test_reconcile_config_is_strict_and_bounded(tmp_path):
    with pytest.raises(ConfigInvalidError, match="max_rounds"):
        Config.load(write_config(tmp_path, VALID + "\n[reconcile]\nmax_rounds = 0\n"))

    with pytest.raises(ConfigInvalidError, match="remote_report_path"):
        Config.load(write_config(tmp_path, VALID + '\n[reconcile]\nremote_report_path = "relative.json"\n'))

    with pytest.raises(ConfigInvalidError, match="unknown"):
        Config.load(write_config(tmp_path, VALID + "\n[reconcile]\nunknown = true\n"))


def test_serve_config_is_strict_and_bounded(tmp_path):
    cfg = Config.load(
        write_config(
            tmp_path,
            VALID
            + '\n[serve]\nhost = "localhost"\nport = 9000\nauth = "none"\n'
            + 'cors_origins = ["http://ui.test"]\n',
        )
    )
    assert cfg.serve.host == "localhost"
    assert cfg.serve.port == 9000
    assert cfg.serve.auth == "none"
    assert cfg.serve.cors_origins == ["http://ui.test"]

    with pytest.raises(ConfigInvalidError, match="loopback"):
        Config.load(write_config(tmp_path, VALID + '\n[serve]\nhost = "0.0.0.0"\nauth = "none"\n'))
    with pytest.raises(ConfigInvalidError, match="port"):
        Config.load(write_config(tmp_path, VALID + "\n[serve]\nport = 70000\n"))
    with pytest.raises(ConfigInvalidError, match="unknown"):
        Config.load(write_config(tmp_path, VALID + "\n[serve]\nunknown = true\n"))
    with pytest.raises(ConfigInvalidError, match="token"):
        Config.load(write_config(tmp_path, VALID + '\n[serve]\ntoken = "plaintext"\n'))


def test_serve_resolve_token_from_env_or_file(tmp_path, monkeypatch):
    cfg = Config.load(write_config(tmp_path))
    monkeypatch.setenv("NCTL_SERVE_TOKEN", "serve-env")
    assert cfg.serve.resolve_token() == "serve-env"

    token_file = tmp_path / "serve-token"
    token_file.write_text("serve-file\n")
    serve = cfg.serve.model_copy(update={"token_file": token_file})
    assert serve.resolve_token() == "serve-file"


def test_ssh_config_defaults_when_section_absent(tmp_path):
    cfg = Config.load(write_config(tmp_path))
    assert cfg.ssh.known_hosts_file == Path("~/.local/state/nctl/ssh/known_hosts")
    assert cfg.ssh.keyscan_timeout_seconds == 10.0
    assert cfg.ssh.lock_path == Path("~/.local/state/nctl/ssh.lock")
    assert cfg.resolved_ssh_known_hosts_file() == Path("~/.local/state/nctl/ssh/known_hosts").expanduser()
    assert cfg.resolved_ssh_lock_path() == Path("~/.local/state/nctl/ssh.lock").expanduser()


def test_ssh_config_overrides_and_path_expansion(tmp_path):
    cfg = Config.load(
        write_config(
            tmp_path,
            VALID
            + '\n[ssh]\nknown_hosts_file = "~/custom/known_hosts"\n'
            + "keyscan_timeout_seconds = 30\n"
            + 'lock_path = "~/custom/ssh.lock"\n',
        )
    )
    assert cfg.ssh.keyscan_timeout_seconds == 30
    assert cfg.resolved_ssh_known_hosts_file() == Path("~/custom/known_hosts").expanduser()
    assert cfg.resolved_ssh_lock_path() == Path("~/custom/ssh.lock").expanduser()


def test_ssh_config_relative_path_resolves_against_config_file_directory(tmp_path, monkeypatch):
    config_dir = tmp_path / "cfgdir"
    config_dir.mkdir()
    cfg = Config.load(
        write_config(
            config_dir,
            VALID + '\n[ssh]\nknown_hosts_file = "state/ssh/known_hosts"\nlock_path = "state/ssh.lock"\n',
        )
    )
    other_cwd = tmp_path / "elsewhere"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)
    assert cfg.resolved_ssh_known_hosts_file() == (config_dir / "state/ssh/known_hosts").resolve()
    assert cfg.resolved_ssh_lock_path() == (config_dir / "state/ssh.lock").resolve()
    assert cfg.resolved_ssh_known_hosts_file().is_absolute()


def test_ssh_config_absolute_path_stays_absolute_regardless_of_config_dir(tmp_path):
    known_hosts = tmp_path / "abs" / "known_hosts"
    cfg = Config.load(
        write_config(tmp_path, VALID + f'\n[ssh]\nknown_hosts_file = "{known_hosts}"\n')
    )
    assert cfg.resolved_ssh_known_hosts_file() == known_hosts


def test_ssh_config_path_with_spaces(tmp_path):
    spaced_dir = tmp_path / "dir with spaces"
    spaced_dir.mkdir()
    cfg = Config.load(
        write_config(spaced_dir, VALID + '\n[ssh]\nknown_hosts_file = "state/known hosts file"\n')
    )
    assert cfg.resolved_ssh_known_hosts_file() == (spaced_dir / "state/known hosts file").resolve()


def test_config_source_path_is_always_absolute_for_relative_explicit_path(tmp_path, monkeypatch):
    write_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    cfg = Config.load(Path("nctl.toml"))
    assert cfg.source_path.is_absolute()
    assert cfg.source_path == (tmp_path / "nctl.toml").resolve()


def test_ssh_config_rejects_bad_timeout_bounds(tmp_path):
    with pytest.raises(ConfigInvalidError, match="keyscan_timeout_seconds"):
        Config.load(write_config(tmp_path, VALID + "\n[ssh]\nkeyscan_timeout_seconds = 0\n"))
    with pytest.raises(ConfigInvalidError, match="keyscan_timeout_seconds"):
        Config.load(write_config(tmp_path, VALID + "\n[ssh]\nkeyscan_timeout_seconds = 121\n"))


def test_ssh_config_rejects_unknown_key(tmp_path):
    with pytest.raises(ConfigInvalidError, match="unknown"):
        Config.load(write_config(tmp_path, VALID + "\n[ssh]\nunknown = true\n"))


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
