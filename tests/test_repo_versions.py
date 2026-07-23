import subprocess

import pytest

from nctl_core.config import Config
from nctl_core.repo_versions import RepoVersionError, resolve_gitlink_commit, resolve_nodeutils_version


def _config(tmp_path, *, nodeutils_version=None):
    reconcile = {}
    if nodeutils_version is not None:
        reconcile["nodeutils_version"] = nodeutils_version
    return Config.model_validate(
        {
            "nautobot": {"url": "http://nautobot.invalid"},
            "inventory": {"dumps_dir": tmp_path / "dumps"},
            "ansible": {"playbook_dir": "ansible", "inventory": "inventory.yml"},
            "repo": {"root": tmp_path},
            "reconcile": reconcile,
            "source_path": tmp_path / "nctl.toml",
        }
    )


def test_resolve_gitlink_commit_reads_superproject_gitlink(tmp_path):
    commit = "1" * 40

    def fake_run(args, **kwargs):
        assert args == ["git", "ls-tree", "HEAD", "--", "nodeutils"]
        assert kwargs["cwd"] == tmp_path
        return subprocess.CompletedProcess(args, 0, f"160000 commit {commit}\tnodeutils\n", "")

    import nctl_core.repo_versions as module

    original = module.subprocess.run
    module.subprocess.run = fake_run
    try:
        assert resolve_gitlink_commit(tmp_path, "nodeutils") == commit
    finally:
        module.subprocess.run = original


def test_resolve_gitlink_commit_rejects_non_gitlink(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "nctl_core.repo_versions.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "100644 blob " + "2" * 40 + "\tnodeutils\n", ""),
    )

    with pytest.raises(RepoVersionError, match="not a gitlink"):
        resolve_gitlink_commit(tmp_path, "nodeutils")


def test_configured_nodeutils_version_works_without_superproject(tmp_path):
    commit = "a" * 40
    assert resolve_nodeutils_version(_config(tmp_path, nodeutils_version=commit)) == commit


def test_nodeutils_version_requires_full_object_id(tmp_path):
    with pytest.raises(ValueError, match="full 40- or 64-character"):
        _config(tmp_path, nodeutils_version="HEAD")
