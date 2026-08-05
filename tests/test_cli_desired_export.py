"""CLI contract for `nctl desired export` (state_bundle Step 1).

Owns the presentation boundary: raw YAML on stdout by default (so shell
redirection produces the snapshot file), the versioned envelope with
`--json`, and a non-zero exit with no partial document on failure.
"""

import json

import yaml
from typer.testing import CliRunner

import nctl_core.cli.main as main
from nctl_core.desired_export import EXPORT_SCHEMA, DesiredExportData, document_counts
from nctl_core.output import Envelope, EnvelopeError

runner = CliRunner()

DOCUMENT = {
    "dry_run": True,
    "operations": [
        {"op": "upsert", "kind": "desired_node", "key": {"slug": "agpc"},
         "values": {"name": "agpc", "slug": "agpc", "node_type": "device", "lifecycle": "active",
                    "role": None, "accepted_actual_types": [], "expected_spec": {}, "realized_device": None}},
    ],
}


def _ok_envelope(*_args, **_kwargs):
    data = DesiredExportData(document=DOCUMENT, counts=document_counts(DOCUMENT), operation_count=1)
    return Envelope.build(EXPORT_SCHEMA, data, [])


def _error_envelope(*_args, **_kwargs):
    return Envelope.build(EXPORT_SCHEMA, DesiredExportData(), [
        EnvelopeError(code="unresolved_reference", message="dangling reference"),
    ])


def test_default_output_is_the_raw_reapplyable_yaml_document(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_desired_export", _ok_envelope)

    result = runner.invoke(main.app, ["desired", "export"])

    assert result.exit_code == 0
    assert yaml.safe_load(result.stdout) == DOCUMENT


def test_json_mode_wraps_the_document_in_the_versioned_envelope(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_desired_export", _ok_envelope)

    result = runner.invoke(main.app, ["desired", "export", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema"] == EXPORT_SCHEMA
    assert payload["ok"] is True
    assert payload["data"]["document"] == DOCUMENT
    assert payload["data"]["operation_count"] == 1


def test_failure_exits_nonzero_and_emits_no_partial_document(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "build_desired_export", _error_envelope)

    result = runner.invoke(main.app, ["desired", "export"])

    assert result.exit_code == 1
    assert "unresolved_reference" in result.stdout
    assert "operations" not in result.stdout
