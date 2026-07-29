"""The sole nctl client for desired-state batch mutations."""

from __future__ import annotations

from typing import Any

from nctl_core.nautobot import NautobotClient, NautobotError

BATCH_PATH = "/api/plugins/intent-catalog/desired-state/batch/"


class DesiredWriteError(NautobotError):
    def __init__(self, message: str, *, status_code: int, artifact: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self.artifact = artifact or {}
        super().__init__(message)


def submit_batch(client: NautobotClient, operations: list[dict[str, Any]], *, dry_run: bool = False) -> dict[str, Any]:
    response = client.rest_post(BATCH_PATH, {"dry_run": dry_run, "operations": operations})
    try:
        artifact = response.json()
    except ValueError:
        artifact = {}
    if response.status_code != 200:
        raise DesiredWriteError(f"desired-state batch failed: HTTP {response.status_code}", status_code=response.status_code, artifact=artifact)
    if not isinstance(artifact, dict) or not isinstance(artifact.get("transaction"), dict):
        raise DesiredWriteError("desired-state batch returned no transaction artifact", status_code=response.status_code, artifact=artifact)
    if not dry_run and artifact["transaction"].get("status") != "committed":
        raise DesiredWriteError("desired-state batch was not committed", status_code=response.status_code, artifact=artifact)
    return artifact


def operation_action(artifact: dict[str, Any], index: int = 0) -> str:
    operations = artifact.get("operations") or []
    if len(operations) <= index or not isinstance(operations[index], dict):
        raise DesiredWriteError("desired-state batch omitted an operation result", status_code=200, artifact=artifact)
    return str(operations[index].get("action") or "")
