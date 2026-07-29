"""Ledger reconciler execution (Phase 4 Step 6, Decision 5).

Planning (Step 5, `planner.py`/`reconcilers.py`) never mutates anything; the
two functions here are the only places `nctl reconcile` writes to the
ledger, matching Decision 5 exactly:

- `execute_link_actual_node` -- one REST PATCH of `realized_device` on a
  `DesiredNode` row through nintent's existing ViewSet, guarded by a
  GraphQL precondition check (never clear or replace an existing link) and a
  post-PATCH GraphQL refetch that asserts the exact link landed. (VM p3 Step 5: the
  legacy `DesiredNode.realized_vm` field was removed outright, so this can no
  longer link a `virtualization.virtualmachine` candidate onto a bare
  `DesiredNode` -- VM linking is now `DesiredComputeInstance.realized_vm`, a
  different object/endpoint, out of scope until a later compute-linking
  phase.)
- `execute_reconcile_ipam` -- triggers the retained "Reconcile Desired IPAM
  Intent" Job (host-scoped via its Step 6 `desired_node` parameter),
  requires the Job to succeed, downloads and validates its versioned
  `nctl.ipam.reconcile.summary.v1` artifact, and verifies every plan row
  stayed inside the requested scope. Conflicts/skips inside a successful Job
  run are returned, not swallowed -- Step 7's executor turns them into
  manual-review findings rather than reporting the action as converged.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from nctl_core.jobs import NautobotJobResult, NautobotJobRunner
from nctl_core.nautobot import NautobotClient, NautobotError
from nctl_core.sources.desired import DesiredComputeInstance, DesiredComputePlatform, DesiredNode, fetch_desired_snapshot

from .model import ReconcileAction

INTENT_API_BASE = "/api/plugins/intent-catalog"
IPAM_JOB_NAME = "Reconcile Desired IPAM Intent"
IPAM_SUMMARY_ARTIFACT_NAME = "ipam-reconcile-summary.json"
IPAM_SUMMARY_SCHEMA_VERSION = "nctl.ipam.reconcile.summary.v1"

# The only actual-object type that the DesiredNode ledger writer can persist.
# Keep this shared with planning so an executor-known invalid candidate never
# becomes an automatic action.
NODE_LINK_CANDIDATE_FIELD_BY_OBJECT_TYPE = {
    "dcim.device": "realized_device",
}


class LedgerActionError(NautobotError):
    def __init__(
        self,
        code: str,
        message: str,
        detail: dict[str, Any] | None = None,
        *,
        mutated: bool = False,
    ) -> None:
        self.code = code
        self.detail = detail or {}
        # The writer that crossed the external mutation boundary owns this
        # evidence. Callers must not infer it from a planned action or an
        # error-code allowlist.
        self.mutated = mutated
        super().__init__(message)


class LinkActualNodeResult(BaseModel):
    node_id: str
    node_slug: str
    field: str
    candidate_id: str
    candidate_name: str = ""


class ComputeLinkResult(BaseModel):
    node_slug: str
    compute_platform_id: str
    cluster_id: str
    compute_instance_id: str
    virtual_machine_id: str
    vmid: int | None = None
    match_basis: str | None = None
    platform_write: str
    instance_write: str


_IPAM_APPLIED_ACTIONS = frozenset({"create_ip_address_applied", "link_ip_address_applied", "noop"})


class IpamReconcileResult(BaseModel):
    desired_node_slug: str
    job_result: NautobotJobResult
    summary: dict[str, Any]
    conflicts: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    eligible_endpoint_ids: list[str] = []
    applied_endpoint_ids: list[str] = []
    unresolved_expected_endpoints: list[dict[str, Any]] = []


def execute_link_actual_node(client: NautobotClient, action: ReconcileAction) -> LinkActualNodeResult:
    """PATCH `realized_device`, then refetch via GraphQL and assert it landed.

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
    field = NODE_LINK_CANDIDATE_FIELD_BY_OBJECT_TYPE.get(candidate.get("object_type"))
    if field is None:
        raise LedgerActionError(
            "unsupported_candidate_type", f"unsupported candidate object_type {candidate.get('object_type')!r}"
        )
    candidate_id = str(candidate.get("id") or "")
    if not candidate_id:
        raise LedgerActionError("missing_candidate_id", "link_actual_node action's candidate has no id")

    before = _get_desired_node_by_id(client, node_id, expected_slug=target.slug)
    if before.realized_device_id or before.realized_device_source:
        raise LedgerActionError(
            "node_already_linked",
            f"DesiredNode {target.slug!r} already has a realized link; refusing to replace it",
            {"before": {"realized_device_id": before.realized_device_id, "realized_device_source": before.realized_device_source}},
        )

    source_field = f"{field}_source"
    response = client.rest_patch(
        f"{INTENT_API_BASE}/nodes/{node_id}/",
        {field: candidate_id, source_field: "derived"},
    )
    if not response.is_success:
        raise LedgerActionError(
            "node_link_patch_failed",
            f"PATCH {field}={candidate_id!r} on DesiredNode {target.slug!r} failed: HTTP {response.status_code}",
            {"status_code": response.status_code, "body": response.text[:200]},
        )

    # A successful REST response is the mutation boundary. Every error from
    # the mandatory GraphQL confirmation below must preserve that fact while
    # still failing the action closed: confirmation is part of success.
    try:
        after = _get_desired_node_by_id(client, node_id, expected_slug=target.slug)
    except LedgerActionError as exc:
        raise LedgerActionError(exc.code, str(exc), exc.detail, mutated=True) from exc
    if after.realized_device_id != candidate_id:
        raise LedgerActionError(
            "node_link_not_confirmed",
            f"expected DesiredNode {target.slug!r}.{field}={candidate_id!r}, refetch shows {after.realized_device_id!r}",
            {"after": after.realized_device_id},
            mutated=True,
        )
    if after.realized_device_source != "derived":
        raise LedgerActionError(
            "node_link_source_not_confirmed",
            f"expected DesiredNode {target.slug!r}.{source_field}='derived'",
            {"after": after.realized_device_source},
            mutated=True,
        )

    return LinkActualNodeResult(
        node_id=node_id,
        node_slug=target.slug or "",
        field=field,
        candidate_id=candidate_id,
        candidate_name=str(candidate.get("name") or ""),
    )


def execute_link_compute_realization(client: NautobotClient, action: ReconcileAction) -> ComputeLinkResult:
    """Write platform then instance links, confirming each write by GraphQL.

    Existing correct links are idempotent; links to any other object are never
    replaced.  Once either PATCH succeeds every later error carries
    ``mutated=True`` so durable operation evidence preserves partial progress.
    """
    if action.reconciler_id != "link_compute_realization":
        raise LedgerActionError("wrong_action", f"not a link_compute_realization action: {action.reconciler_id!r}")
    p = action.parameters
    platform_id = str(p.get("compute_platform_id") or "")
    instance_id = str(p.get("compute_instance_id") or "")
    cluster_id = str(p.get("cluster_id") or "")
    vm_id = str(p.get("virtual_machine_id") or "")
    if not all((platform_id, instance_id, cluster_id, vm_id)):
        raise LedgerActionError("missing_compute_link_parameter", "compute link action has incomplete pinned identities")
    mutated = False
    try:
        desired = fetch_desired_snapshot(client)
        platform = next((row for row in desired.compute_platforms if row.id == platform_id), None)
        instance = next((row for row in desired.compute_instances if row.id == instance_id), None)
        if platform is None or instance is None:
            raise LedgerActionError("compute_link_fetch_failed", "planned compute platform or instance no longer exists")
        if platform.realized_cluster_id not in (None, cluster_id):
            raise LedgerActionError("platform_link_already_other", "platform link points to a different Cluster; refusing to replace it")
        if instance.realized_vm_id not in (None, vm_id):
            raise LedgerActionError("instance_link_already_other", "instance link points to a different VirtualMachine; refusing to replace it")
        platform_write = "already_correct"
        if platform.realized_cluster_id is None:
            _patch_compute_link(client, "compute-platforms", platform_id, "realized_cluster", cluster_id)
            mutated = True
            platform_write = "patched"
        after_platform = _get_compute_rows(client, platform_id, instance_id)[0]
        if after_platform.realized_cluster_id != cluster_id or after_platform.realized_cluster_source != "derived":
            raise LedgerActionError("platform_link_not_confirmed", "platform link PATCH was not confirmed by GraphQL", mutated=mutated)
        instance_write = "already_correct"
        if instance.realized_vm_id is None:
            _patch_compute_link(client, "compute-instances", instance_id, "realized_vm", vm_id)
            mutated = True
            instance_write = "patched"
        _after_platform, after_instance = _get_compute_rows(client, platform_id, instance_id)
        if after_instance.realized_vm_id != vm_id or after_instance.realized_vm_source != "derived":
            raise LedgerActionError("instance_link_not_confirmed", "instance link PATCH was not confirmed by GraphQL", mutated=mutated)
    except LedgerActionError as exc:
        if exc.mutated or not mutated:
            raise
        raise LedgerActionError(exc.code, str(exc), exc.detail, mutated=True) from exc
    target = action.targets[0]
    return ComputeLinkResult(
        node_slug=target.slug or "", compute_platform_id=platform_id, cluster_id=cluster_id,
        compute_instance_id=instance_id, virtual_machine_id=vm_id, vmid=p.get("vmid"),
        match_basis=p.get("match_basis"), platform_write=platform_write, instance_write=instance_write,
    )


def _patch_compute_link(client: NautobotClient, collection: str, object_id: str, field: str, value: str) -> None:
    response = client.rest_patch(f"{INTENT_API_BASE}/{collection}/{object_id}/", {field: value, f"{field}_source": "derived"})
    if not response.is_success:
        raise LedgerActionError("compute_link_patch_failed", f"PATCH {field} on {collection}/{object_id} failed: HTTP {response.status_code}", {"status_code": response.status_code, "body": response.text[:200]})


def _get_compute_rows(client: NautobotClient, platform_id: str, instance_id: str) -> tuple[DesiredComputePlatform, DesiredComputeInstance]:
    try:
        desired = fetch_desired_snapshot(client)
    except Exception as exc:
        raise LedgerActionError("compute_link_fetch_failed", f"cannot refetch desired compute rows: {exc}") from exc
    platform = next((row for row in desired.compute_platforms if row.id == platform_id), None)
    instance = next((row for row in desired.compute_instances if row.id == instance_id), None)
    if platform is None or instance is None:
        raise LedgerActionError("compute_link_fetch_failed", "compute platform or instance is absent after PATCH")
    return platform, instance


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

    eligible_endpoint_ids = [str(value) for value in (action.evidence.get("eligible_endpoint_ids") or [])]
    plan_by_endpoint_id = {
        str(plan["desired_endpoint"]["id"]): plan
        for plan in plans
        if isinstance(plan.get("desired_endpoint"), dict) and plan["desired_endpoint"].get("id")
    }
    if eligible_endpoint_ids:
        missing_ids = [eid for eid in eligible_endpoint_ids if eid not in plan_by_endpoint_id]
        if missing_ids:
            raise LedgerActionError(
                "ipam_summary_coverage_mismatch",
                f"eligible endpoint id(s) {missing_ids} pinned at plan time are missing from the "
                f"Job's plan rows for {node_slug!r}",
                {"missing_endpoint_ids": missing_ids, "eligible_endpoint_ids": eligible_endpoint_ids},
            )
        endpoints_count = (summary.get("summary") or {}).get("endpoints")
        if endpoints_count is not None and endpoints_count != len(plans):
            raise LedgerActionError(
                "ipam_summary_coverage_mismatch",
                f"summary.endpoints={endpoints_count} disagrees with the plan-row count {len(plans)}",
                {"endpoints_count": endpoints_count, "plan_row_count": len(plans)},
            )
    elif not plans:
        raise LedgerActionError(
            "ipam_summary_coverage_mismatch",
            f"Job {IPAM_JOB_NAME!r} plan artifact contains zero endpoints for {node_slug!r}",
        )

    conflicts = [plan for plan in plans if plan.get("action") == "conflict"]
    skipped = [plan for plan in plans if plan.get("action") == "skip"]
    expected_plans = [plan_by_endpoint_id[eid] for eid in eligible_endpoint_ids] if eligible_endpoint_ids else plans
    applied_endpoint_ids = [
        str(plan["desired_endpoint"].get("id") or "")
        for plan in expected_plans
        if plan.get("action") in _IPAM_APPLIED_ACTIONS and isinstance(plan.get("desired_endpoint"), dict)
    ]
    applied_endpoint_ids = [value for value in applied_endpoint_ids if value]
    unresolved_expected_endpoints = [plan for plan in expected_plans if plan.get("action") not in _IPAM_APPLIED_ACTIONS]
    return IpamReconcileResult(
        desired_node_slug=node_slug,
        job_result=job_result,
        summary=summary,
        conflicts=conflicts,
        skipped=skipped,
        eligible_endpoint_ids=eligible_endpoint_ids,
        applied_endpoint_ids=applied_endpoint_ids,
        unresolved_expected_endpoints=unresolved_expected_endpoints,
    )


def _get_desired_node_by_id(client: NautobotClient, node_id: str, expected_slug: str | None = None) -> DesiredNode:
    try:
        snapshot = fetch_desired_snapshot(client)
    except Exception as exc:
        raise LedgerActionError(
            "node_fetch_failed",
            f"cannot fetch desired snapshot for DesiredNode {node_id}: {exc}",
        ) from exc

    node = next((n for n in snapshot.nodes if n.id == node_id), None)
    if node is None:
        raise LedgerActionError(
            "node_fetch_failed",
            f"DesiredNode {node_id} not found in GraphQL desired snapshot",
            {"node_id": node_id},
        )
    if expected_slug and node.slug != expected_slug:
        raise LedgerActionError(
            "node_fetch_mismatch",
            f"DesiredNode {node_id} has slug {node.slug!r}, expected {expected_slug!r}",
            {"found_slug": node.slug, "expected_slug": expected_slug},
        )
    return node


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
