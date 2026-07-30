import json

from typer.testing import CliRunner

import nctl_core.cli.main as main
from nctl_core.desired_write import DesiredWriteError


runner = CliRunner()

ARTIFACT = {
    "schema_version": "nintent.desired-state-batch.v1",
    "errors": [],
    "totals": {"create": 3, "update": 0, "delete": 0, "unchanged": 0, "conflict": 0},
    "operations": [
        {"index": 0, "kind": "desired_node", "action": "create", "reason": None},
        {"index": 1, "kind": "desired_endpoint", "action": "create", "reason": None},
        {"index": 2, "kind": "desired_compute_instance", "action": "create", "reason": None},
    ],
    "transaction": {
        "status": "rolled_back",
        "committed": False,
        "error": "ValidationError: {'__all__': ['compute_primary_endpoint_missing']}",
    },
}


def _rejecting_apply(*_args, **_kwargs):
    raise DesiredWriteError("desired-state batch failed: HTTP 409", status_code=409, artifact=ARTIFACT)


def test_desired_apply_json_emits_complete_409_artifact_and_fails(monkeypatch):
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(main, "apply_document", _rejecting_apply)

    result = runner.invoke(main.app, ["desired", "apply", "-f", "ignored.yaml", "--yes", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout) == ARTIFACT


def test_desired_apply_text_emits_transaction_and_conflict_reason_and_fails(monkeypatch):
    artifact = {**ARTIFACT, "operations": [
        *ARTIFACT["operations"][:2],
        {"index": 2, "kind": "desired_compute_instance", "action": "conflict", "reason": "compute_primary_endpoint_missing"},
    ]}
    monkeypatch.setattr(main, "_load_config", lambda path: object())
    monkeypatch.setattr(
        main,
        "apply_document",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            DesiredWriteError("desired-state batch failed: HTTP 409", status_code=409, artifact=artifact)
        ),
    )

    result = runner.invoke(main.app, ["desired", "apply", "-f", "ignored.yaml", "--yes"])

    assert result.exit_code == 1
    assert "HTTP 409" in result.output
    assert "compute_primary_endpoint_missing" in result.output
