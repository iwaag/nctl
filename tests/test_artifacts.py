import json
import stat

import pytest

from nctl_core.artifacts import ArtifactError, OperationArtifacts


def test_operation_artifacts_are_private_and_atomic(tmp_path):
    artifacts = OperationArtifacts.create(tmp_path / "events", "01JTEST")
    path = artifacts.write_json("jobs/result.json", {"ok": True})

    assert json.loads(path.read_text()) == {"ok": True}
    assert stat.S_IMODE(artifacts.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(path.parent.glob(f".{path.name}.*")) == []


@pytest.mark.parametrize("relative", ["../escape", "/tmp/escape", "nested/../../escape"])
def test_operation_artifacts_reject_path_escape(tmp_path, relative):
    artifacts = OperationArtifacts.create(tmp_path / "events", "01JTEST")
    with pytest.raises(ArtifactError, match="artifact path"):
        artifacts.write_text(relative, "nope")


def test_operation_artifacts_fail_preflight_when_probe_cannot_be_written(tmp_path, monkeypatch):
    artifacts = OperationArtifacts(tmp_path / "events/01JTEST")

    def deny(_destination, _content):
        raise PermissionError("denied")

    monkeypatch.setattr(artifacts, "_atomic_write", deny)
    with pytest.raises(ArtifactError, match="cannot establish operation artifact directory"):
        artifacts.ensure_writable()
