import json

from typer.testing import CliRunner

import nctl_core.cli.main as main
from nctl_core.config import Config

runner = CliRunner()


def _config(tmp_path, *, auth="token"):
    return Config.model_validate(
        {
            "nautobot": {"url": "http://nautobot.test"},
            "inventory": {},
            "ansible": {"playbook_dir": tmp_path, "inventory": "inventory.yml"},
            "serve": {"auth": auth},
            "source_path": tmp_path / "nctl.toml",
        }
    )


def test_serve_prints_startup_envelope_then_runs_uvicorn(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    monkeypatch.setenv("NCTL_SERVE_TOKEN", "test-token")
    monkeypatch.setattr(main, "_load_config", lambda path: cfg)
    called = []
    monkeypatch.setattr(main, "run_server", lambda value: called.append(value))

    result = runner.invoke(main.app, ["serve", "--host", "localhost", "--port", "9000", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema"] == "nctl.serve.v1"
    assert payload["data"] == {
        "host": "localhost",
        "port": 9000,
        "auth": "token",
        "dashboard_url": "http://localhost:9000/",
    }
    assert called[0].serve.host == "localhost"
    assert called[0].serve.port == 9000


def test_serve_refuses_missing_token_before_startup_message(tmp_path, monkeypatch):
    monkeypatch.delenv("NCTL_SERVE_TOKEN", raising=False)
    monkeypatch.setattr(main, "_load_config", lambda path: _config(tmp_path))
    monkeypatch.setattr(main, "run_server", lambda cfg: (_ for _ in ()).throw(AssertionError("must not run")))
    result = runner.invoke(main.app, ["serve"])
    assert result.exit_code == 2
    assert result.stdout == ""
    assert "no token" in result.stderr


def test_serve_host_override_revalidates_none_auth(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: _config(tmp_path, auth="none"))
    result = runner.invoke(main.app, ["serve", "--host", "0.0.0.0"])
    assert result.exit_code == 2
    assert "loopback" in result.stderr
