"""Ledger reconciler execution (Phase 4 Step 6, Decision 5).

Planning (Step 5, `planner.py`/`reconcilers.py`) never mutates anything; the
two functions here are the only places `nctl reconcile` writes to the
ledger, matching Decision 5 exactly:

- `execute_link_actual_node` -- one REST PATCH of `realized_device` or
  `realized_vm` on a `DesiredNode` row through nintent's existing ViewSet,
  guarded by a precondition check (never clear or replace an existing link)
  and a post-PATCH refetch that asserts the exact link landed.
- `execute_reconcile_ipam` -- triggers the retained "Reconcile Desired IPAM
  Intent" Job (host-scoped via its Step 6 `desired_node` parameter),
  requires the Job to succeed, downloads and validates its versioned
  `nctl.ipam.reconcile.summary.v1` artifact, and verifies every plan row
  stayed inside the requested scope. Conflicts/skips inside a successful Job
  run are returned, not swallowed -- Step 7's executor turns them into
  manual-review findings rather than reporting the action as converged.

Neither function is closed-loop verified against a live Nautobot yet: Step 3
recorded a 403 against the local dev instance before the intent-catalog
token/config was refreshed. Both are implemented against the serializer/Job
contracts documented in `p4/plan.md`'s "Risks to verify first" and exercised
here with a fake `NautobotClient`/`NautobotJobRunner`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from nctl_core.jobs import NautobotJobResult, NautobotJobRunner
from nctl_core.nautobot import NautobotClient, NautobotError

from .model import ReconcileAction

INTENT_API_BASE = "/api/plugins/intent-catalog"
IPAM_JOB_NAME = "Reconcile Desired IPAM Intent"
IPAM_SUMMARY_ARTIFACT_NAME = "ipam-reconcile-summary.json"
IPAM_SUMMARY_SCHEMA_VERSION = "nctl.ipam.reconcile.summary.v1"

_CANDIDATE_FIELD_BY_OBJECT_TYPE = {
    "dcim.device": "realized_device",
    "virtualization.virtualmachine": "realized_vm",
}


class LedgerActionError(NautobotError):
    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None) -> None:
        self.code = code
        self.detail = detail or {}
        super().__init__(message)


class LinkActualNodeResult(BaseModel):
    node_id: str
    node_slug: str
    field: str
    candidate_id: str
    candidate_name: str = ""


class IpamReconcileResult(BaseModel):
    desired_node_slug: str
    job_result: NautobotJobResult
    summary: dict[str, Any]
    conflicts: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []


def execute_link_actual_node(client: NautobotClient, action: ReconcileAction) -> LinkActualNodeResult:
    """PATCH exactly one of `realized_device`/`realized_vm`, then refetch and assert it landed.

    Never clears or replaces an existing link: if the row already has either
    field set, this raises rather than PATCHing over it (Decision 5).
    """

    if action.reconciler_id != "link_actual_node":
        raise LedgerActionError("wrong_action", f"not a link_actual_node action: {action.reconciler_id!r}")
    target = action.targets[0]
    node_id = target.id
    if not node_id:
        raise LedgerActionError("missing_target_id", "link_actual_node action has no target id")

    candidate = action.parameters.get("candidate") or {}
    field = _CANDIDATE_FIELD_BY_OBJECT_TYPE.get(candidate.get("object_type"))
    if field is None:
        raise LedgerActionError(
            "unsupported_candidate_type", f"unsupported candidate object_type {candidate.get('object_type')!r}"
        )
    candidate_id = str(candidate.get("id") or "")
    if not candidate_id:
        raise LedgerActionError("missing_candidate_id", "link_actual_node action's candidate has no id")

    before = _get_node(client, node_id)
    if _linked_id(before.get("realized_device")) or _linked_id(before.get("realized_vm")):
        raise LedgerActionError(
            "node_already_linked",
            f"DesiredNode {target.slug!r} already has a realized link; refusing to replace it",
            {"before": {"realized_device": before.get("realized_device"), "realized_vm": before.get("realized_vm")}},
        )

    response = client.rest_patch(f"{INTENT_API_BASE}/nodes/{node_id}/", {field: candidate_id})
    if not response.is_success:
        raise LedgerActionError(
            "node_link_patch_failed",
            f"PATCH {field}={candidate_id!r} on DesiredNode {target.slug!r} failed: HTTP {response.status_code}",
            {"status_code": response.status_code, "body": response.text[:200]},
        )

    after = _get_node(client, node_id)
    linked_id = _linked_id(after.get(field))
    if linked_id != candidate_id:
        raise LedgerActionError(
            "node_link_not_confirmed",
            f"expected DesiredNode {target.slug!r}.{field}={candidate_id!r}, refetch shows {linked_id!r}",
            {"after": after.get(field)},
        )

    return LinkActualNodeResult(
        node_id=node_id,
        node_slug=target.slug or "",
        field=field,
        candidate_id=candidate_id,
        candidate_name=str(candidate.get("name") or ""),
    )


def execute_reconcile_ipam(
    job_runner: NautobotJobRunner,
    action: ReconcileAction,
    *,
    artifact_relative_path: str | Path,
) -> IpamReconcileResult:
    """Trigger the retained IPAM Job scoped to one node and validate its summary artifact."""

    if action.reconciler_id != "reconcile_ipam":
        raise LedgerActionError("wrong_action", f"not a reconcile_ipam action: {action.reconciler_id!r}")
    node_slug = str(action.parameters.get("desired_node_slug") or "")
    if not node_slug:
        raise LedgerActionError("missing_node_slug", "reconcile_ipam action has no desired_node_slug parameter")

    job_result = job_runner.run(
        IPAM_JOB_NAME,
        {"commit_changes": True, "include_inactive": False, "desired_node": node_slug},
        artifact_name=IPAM_SUMMARY_ARTIFACT_NAME,
        artifact_relative_path=artifact_relative_path,
    )
    if job_result.artifact_path is None:
        raise LedgerActionError(
            "ipam_summary_missing", f"Job {IPAM_JOB_NAME!r} completed without the summary artifact"
        )
    summary = _read_json(Path(job_result.artifact_path))

    schema_version = summary.get("schema_version")
    if schema_version != IPAM_SUMMARY_SCHEMA_VERSION:
        raise LedgerActionError(
            "ipam_summary_schema_mismatch",
            f"expected summary schema {IPAM_SUMMARY_SCHEMA_VERSION!r}, got {schema_version!r}",
        )

    scope = summary.get("scope") or {}
    selected_slugs = set(scope.get("selected_desired_node_slugs") or [])
    if selected_slugs - {node_slug}:
        raise LedgerActionError(
            "ipam_summary_scope_mismatch",
            f"requested only {node_slug!r} but the Job touched {sorted(selected_slugs)}",
            {"selected_desired_node_slugs": sorted(selected_slugs)},
        )

    plans = summary.get("plans") or []
    out_of_scope = [
        plan
        for plan in plans
        if plan.get("desired_endpoint", {}).get("desired_node_slug") not in (node_slug, "")
    ]
    if out_of_scope:
        raise LedgerActionError(
            "ipam_summary_out_of_scope_rows",
            f"{len(out_of_scope)} summary plan row(s) reference a node other than {node_slug!r}",
        )

    conflicts = [plan for plan in plans if plan.get("action") == "conflict"]
    skipped = [plan for plan in plans if plan.get("action") == "skip"]
    return IpamReconcileResult(
        desired_node_slug=node_slug,
        job_result=job_result,
        summary=summary,
        conflicts=conflicts,
        skipped=skipped,
    )


def _get_node(client: NautobotClient, node_id: str) -> dict[str, Any]:
    response = client.rest_get(f"{INTENT_API_BASE}/nodes/{node_id}/")
    if not response.is_success:
        raise LedgerActionError(
            "node_fetch_failed",
            f"cannot fetch DesiredNode {node_id}: HTTP {response.status_code}",
            {"status_code": response.status_code},
        )
    body = response.json()
    if not isinstance(body, dict):
        raise LedgerActionError("node_fetch_invalid", f"DesiredNode {node_id} response is not an object")
    return body


def _linked_id(value: Any) -> str | None:
    """Normalize a serialized FK value (nested object, plain id, or null) to an id or None."""

    if value in (None, "", {}):
        return None
    if isinstance(value, dict):
        linked = value.get("id") or value.get("pk")
        return str(linked) if linked else None
    return str(value)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text()
    except OSError as exc:
        raise LedgerActionError("ipam_summary_unreadable", f"cannot read {path}: {exc}") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LedgerActionError("ipam_summary_invalid_json", f"cannot parse {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise LedgerActionError("ipam_summary_invalid_json", f"{path} root is not an object")
    return payload
