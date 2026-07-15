from __future__ import annotations

import subprocess
from pathlib import Path

from nctl_core.output import Envelope, EnvelopeError
from nctl_core.production_render import ProductionRenderData, write_production_artifacts

SCHEMA = "nctl.render.production.v1"


def _ok_envelope(generation_id: str = "gen-1") -> Envelope[ProductionRenderData]:
    data = ProductionRenderData(
        inventory={"all": {"vars": {}, "children": {}}},
        report={"generation_id": generation_id, "summary": {"included": 0}},
        inventory_yaml="# inventory\n",
        report_json='{"generation_id": "gen-1"}\n',
    )
    return Envelope.build(SCHEMA, data, [])


def test_write_production_artifacts_propagates_render_failure(tmp_path):
    envelope = Envelope.build(
        SCHEMA, ProductionRenderData(), [EnvelopeError(code="nautobot_fetch_failed", message="boom")]
    )

    error = write_production_artifacts(envelope, tmp_path)

    assert error is not None
    assert error.code == "nautobot_fetch_failed"


def test_write_production_artifacts_requires_ansible_inventory_on_path(tmp_path, monkeypatch):
    monkeypatch.setattr("nctl_core.production_render.shutil.which", lambda name: None)

    error = write_production_artifacts(_ok_envelope(), tmp_path)

    assert error is not None
    assert error.code == "ansible_executable_missing"
    assert not (tmp_path / "production.yml").exists()


def test_write_production_artifacts_atomically_replaces_on_success(tmp_path, monkeypatch):
    monkeypatch.setattr("nctl_core.production_render.shutil.which", lambda name: "/usr/bin/ansible-inventory")
    monkeypatch.setattr(
        "nctl_core.production_render.subprocess.run",
        lambda *a, **kw: subprocess.CompletedProcess(args=a, returncode=0, stdout="{}", stderr=""),
    )

    error = write_production_artifacts(_ok_envelope("gen-42"), tmp_path)

    assert error is None
    assert (tmp_path / "production.yml").read_text() == "# inventory\n"
    assert (tmp_path / "production.reports" / "gen-42.json").read_text() == '{"generation_id": "gen-1"}\n'
    # no leftover staging file
    assert list(tmp_path.glob(".production.yml.*.tmp")) == []


def test_write_production_artifacts_leaves_no_file_when_validation_fails(tmp_path, monkeypatch):
    monkeypatch.setattr("nctl_core.production_render.shutil.which", lambda name: "/usr/bin/ansible-inventory")
    monkeypatch.setattr(
        "nctl_core.production_render.subprocess.run",
        lambda *a, **kw: subprocess.CompletedProcess(args=a, returncode=1, stdout="", stderr="boom"),
    )

    error = write_production_artifacts(_ok_envelope(), tmp_path)

    assert error is not None
    assert error.code == "ansible_inventory_invalid"
    assert not (tmp_path / "production.yml").exists()
    assert list(tmp_path.glob(".production.yml.*.tmp")) == []
