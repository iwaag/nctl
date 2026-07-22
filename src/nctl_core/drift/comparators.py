"""Initial comparators (Phase 2 Step 3).

- `node_existence` — a lightweight, non-fuzzy existence check: does a desired
  node's `realized_device_id`/`realized_vm_id` actually resolve in the actual
  snapshot, and does a node whose operational config requires actual state
  have *any* realized object at all? This is deliberately not the full
  candidate-matching/ranking nintent's `evaluate_node_intent` does (scoring
  unlinked nodes against Device/VM candidates by name/serial) — that fuzzy
  matching is Step 4's evaluation port. Step 3 only checks the links that
  already exist.
- `ingest_lag` — compares each observed nodeutils dump's `collected_at`
  against the actual-backed device's last-ingested `last_seen` custom field
  (via `sources.actual.ActualFacts.collected_at`). A dump newer than the last
  ingest means nauto's `Ingest Nodeutils Inventory` Job hasn't run since the
  node last reported — `info` severity, since it's expected between
  collection and ingest, not necessarily wrong.
- `production_policy` — reuses the Step 2 composer (`compose_production_inventory`,
  which itself reuses `evaluate_platform_policy` and the skip-reason helpers)
  instead of reimplementing platform-policy/freshness logic a second time, so
  the composer's `skipped`/`drift` report entries and these diffs can never
  disagree by construction. A composition-wide `ContractError` (e.g. a
  placement referencing an unknown deployment profile) becomes one
  `kind="global"` diff rather than failing the whole drift run.
- `node_intent_matching` / `endpoint_intent_matching` / `service_intent_matching`
  (Phase 2 Step 4) — thin wrappers around the ported `evaluation.py` pure
  functions (`evaluate_node_intent`/`evaluate_endpoint_intent`/
  `evaluate_service_intent`, via `evaluation_snapshot.py`'s snapshot adapter):
  each gap the evaluator produces becomes one `DiffRecord` whose `code` is the
  gap's code unchanged and whose severity is derived from the gap's nintent
  severity (`conflict`/`missing`/`unknown` -> `error`, `partial`/
  `needs_review` -> `warning`) — see `_SEVERITY_BY_GAP_SEVERITY`. This
  supersedes `node_existence`'s existence-only check with the real fuzzy
  candidate-ranking nintent's Evaluate Jobs did; `node_existence` stays
  registered too (not removed) because it is a strictly faster, narrower
  check (dangling FK / policy-requires-realization) that a reader might want
  independent of the heavier candidate-scoring pass, and removing it would
  make `realized_device_missing`/`realized_vm_missing`/`no_realized_object`
  indistinguishable from the fuzzy-matching codes in the diff stream.
  `endpoint_intent_matching` and `service_intent_matching` are registered
  under their own resource types (`"endpoint"`/`"service"`) for registry
  bookkeeping only — `Target.kind` for an endpoint gap is still `"node"`
  (attributed to the endpoint's owning desired node, matching the seeded
  per-node targets in `engine.py`) since a desired endpoint has no
  independent drift-status lifecycle of its own in the roadmap's vocabulary.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterator

from nctl_core.production.adapter import build_production_node_inputs
from nctl_core.production.composer import (
    ACTIVE_PLACEMENT_NOT_APPLIED,
    ContractError,
    NodeInput,
    NodeOutcome,
    build_node_report_record,
    compose_production_inventory,
    try_resolve_operational_values,
    unapplied_placement_findings,
)
from nctl_core.sources.actual import ActualDevice
from nctl_core.sources.snapshot import SourceSnapshot

from .context import DriftContext
from .evaluation import EvaluationResult
from .evaluation_snapshot import evaluate_all_endpoints, evaluate_all_nodes, evaluate_all_services
from .model import DiffRecord, Severity, Target
from .registry import register

_SEVERITY_BY_GAP_SEVERITY = {
    "conflict": Severity.ERROR,
    "missing": Severity.ERROR,
    "unknown": Severity.ERROR,
    "partial": Severity.WARNING,
    "needs_review": Severity.WARNING,
}

# A canonical placeholder generation id/digest: production_policy discards the
# composed inventory/report's generation-specific fields (only `skipped` and
# `drift` are read), so a real UUID/digest would be pure noise here.
_PLACEHOLDER_GENERATION_ID = "00000000-0000-0000-0000-000000000000"
_PLACEHOLDER_DIGEST = "0" * 64


@register("node")
def node_existence(snapshot: SourceSnapshot, context: DriftContext) -> Iterator[DiffRecord]:
    override_by_node = {item.node_id: item for item in snapshot.desired.operational_overrides}
    devices_by_id = {device.id: device for device in snapshot.actual.devices}
    vms_by_id = {vm.id: vm for vm in snapshot.actual.virtual_machines}

    for node in snapshot.desired.nodes:
        target = Target(kind="node", slug=node.slug, name=node.name, id=node.id)
        operational_override = override_by_node.get(node.id)

        if node.realized_device_id and node.realized_device_id not in devices_by_id:
            yield DiffRecord(
                target=target,
                code="realized_device_missing",
                severity=Severity.ERROR,
                message=(
                    f"{node.slug}: references realized_device {node.realized_device_id!r}, "
                    "which no longer exists in Nautobot"
                ),
                desired={"realized_device_id": node.realized_device_id},
                sources=["desired", "actual"],
            )
        if node.realized_vm_id and node.realized_vm_id not in vms_by_id:
            yield DiffRecord(
                target=target,
                code="realized_vm_missing",
                severity=Severity.ERROR,
                message=(
                    f"{node.slug}: references realized_vm {node.realized_vm_id!r}, "
                    "which no longer exists in Nautobot"
                ),
                desired={"realized_vm_id": node.realized_vm_id},
                sources=["desired", "actual"],
            )
        if (
            (operational_override is None or operational_override.declared_host_os is None)
            and not node.realized_device_id
            and not node.realized_vm_id
        ):
            yield DiffRecord(
                target=target,
                code="no_realized_object",
                severity=Severity.ERROR,
                message=f"{node.slug}: observed operation is required but no realized device or VM is linked",
                desired={"actual_state_policy": "required"},
                sources=["desired"],
            )


@register("node")
def ingest_lag(snapshot: SourceSnapshot, context: DriftContext) -> Iterator[DiffRecord]:
    devices_by_name = {device.name: device for device in snapshot.actual.devices}
    node_by_device_id = {node.realized_device_id: node for node in snapshot.desired.nodes if node.realized_device_id}

    for observed in snapshot.observed:
        device = devices_by_name.get(observed.hostname)
        if device is None:
            continue
        node = node_by_device_id.get(device.id)
        target = (
            Target(kind="node", slug=node.slug, name=node.name, id=node.id)
            if node is not None
            else Target(kind="device", name=device.name, id=device.id)
        )

        diff = _ingest_lag_diff(target, observed.hostname, observed.collected_at, device)
        if diff is not None:
            yield diff


def _ingest_lag_diff(target: Target, hostname: str, observed_at: datetime, device: ActualDevice) -> DiffRecord | None:
    facts = device.actual_facts()
    if facts.collected_at is None:
        return DiffRecord(
            target=target,
            code="ingest_lag",
            severity=Severity.INFO,
            message=f"{hostname}: a nodeutils dump exists but Nautobot has never ingested it",
            actual={"nautobot_last_seen": None, "dump_collected_at": observed_at.isoformat()},
            sources=["observed", "actual"],
        )
    try:
        actual_collected_at = datetime.fromisoformat(facts.collected_at)
    except ValueError:
        return None
    if observed_at.tzinfo is None or observed_at <= actual_collected_at:
        return None
    return DiffRecord(
        target=target,
        code="ingest_lag",
        severity=Severity.INFO,
        message=(
            f"{hostname}: nodeutils dump ({observed_at.isoformat()}) is newer than "
            f"Nautobot's last ingest ({facts.collected_at})"
        ),
        actual={"nautobot_last_seen": facts.collected_at, "dump_collected_at": observed_at.isoformat()},
        sources=["observed", "actual"],
    )


@register("node")
def production_policy(snapshot: SourceSnapshot, context: DriftContext) -> Iterator[DiffRecord]:
    node_inputs = build_production_node_inputs(snapshot)

    # active_placement_not_applied does not depend on profiles at all
    # (Decision 4 of core_reconcile) -- recorded intent must not disappear
    # merely because the profile-dependent composer below is unavailable, so
    # this pure, composer-independent source runs unconditionally.
    for entry in unapplied_placement_findings(node_inputs):
        yield _active_placement_not_applied_diff(entry)

    if context.profiles_error is not None:
        # Phase 4 Decision 3: a missing/unparsable/invalid deployment-profiles
        # file is a classified global blocker, not a silent degrade to `{}`.
        # No node's production state can be established without it, so every
        # node's intent_effect_summary reports `unknown` rather than a guess.
        yield DiffRecord(
            target=Target(kind="global"),
            code="deployment_profiles_unavailable",
            severity=Severity.ERROR,
            message=context.profiles_error,
            sources=["actual"],
        )
        for node_input in node_inputs:
            yield _intent_effect_summary_diff_unknown(node_input, context.generated_at)
        return

    if not context.profiles:
        for node_input in node_inputs:
            yield _intent_effect_summary_diff_unknown(node_input, context.generated_at)
        return

    try:
        composition = compose_production_inventory(
            node_inputs,
            context.profiles,
            generation_id=_PLACEHOLDER_GENERATION_ID,
            generated_at=context.generated_at,
            deployment_profile_digest=_PLACEHOLDER_DIGEST,
        )
    except ContractError as exc:
        yield DiffRecord(
            target=Target(kind="global"),
            code=exc.code,
            severity=Severity.ERROR,
            message=str(exc),
            sources=["desired", "actual"],
        )
        for node_input in node_inputs:
            yield _intent_effect_summary_diff_unknown(node_input, context.generated_at)
        return

    # Report 3.0 (Phase 4 Decision 2/3) carries one closed `nodes` record per
    # desired node instead of parallel `errors`/`skipped`/`drift` collections.
    # Each record becomes exactly one `intent_effect_summary` INFO diff
    # (recorded intent, effective mechanism, and production/placement
    # application in one place), plus every node's `local_findings` (Phase 1
    # Group C, still node-targeted ERROR diffs) and `production.state ==
    # "skipped"` reasons translated into the same actionable-diff shape as
    # before. Structured local errors take priority: each becomes its own
    # precise node-targeted diff, and the generic skip-reason conversion below
    # is suppressed for the exact (node, code) pairs they already cover so the
    # same failure never surfaces twice.
    structured_error_keys: set[tuple[str, str]] = set()
    for node_record in composition.report["nodes"]:
        yield _intent_effect_summary_diff_from_record(node_record)

        identity = node_record["desired"]["node"]
        target = Target(kind="node", slug=identity["slug"], name=identity["name"], id=identity["id"])
        for finding in node_record["actual"]["local_findings"]:
            yield DiffRecord(
                target=target,
                code=finding["code"],
                severity=Severity.ERROR,
                message=finding["message"],
                desired=dict(finding.get("evidence", {})),
                actual={"stage": finding["stage"]},
                sources=["desired", "actual"],
            )
            structured_error_keys.add((identity["slug"], finding["code"]))

        production = node_record["actual"]["production"]
        if production["state"] != "skipped":
            continue
        for reason in production["reasons"]:
            if (identity["slug"], reason) in structured_error_keys:
                continue
            yield DiffRecord(
                target=target,
                code=reason,
                severity=Severity.ERROR,
                message=f"{identity['slug']}: production composition skipped this node ({reason})",
                sources=["desired", "actual"],
            )


def _intent_effect_summary_diff_from_record(node_record: dict) -> DiffRecord:
    """Turn one report-3.0 node record (`production.composer.build_node_report_record`) into
    the `intent_effect_summary` INFO diff (Phase 4 Decision 2): the record's `desired` section
    already *is* the recorded intent, and its `actual` section already *is* the effective
    mechanism plus production/placement application -- this is a pure re-labeling, not a
    second derivation.
    """

    identity = node_record["desired"]["node"]
    return DiffRecord(
        target=Target(kind="node", slug=identity["slug"], name=identity["name"], id=identity["id"]),
        code="intent_effect_summary",
        severity=Severity.INFO,
        message=f"{identity['slug']}: recorded intent, effective mechanism, and production application",
        desired=node_record["desired"],
        actual=node_record["actual"],
        sources=["desired", "actual"],
    )


def _intent_effect_summary_diff_unknown(node_input: NodeInput, generated_at: str) -> DiffRecord:
    """Build one `intent_effect_summary` diff when production composition itself could not
    run (missing/invalid deployment profiles, or a global contract failure) -- the node's
    recorded intent and effective mechanism are still fully computable and worth surfacing,
    but its production/placement application is genuinely `unknown`, not `included`/`skipped`/
    `out_of_scope`, since composition was never attempted.
    """

    effective, finding = try_resolve_operational_values(node_input, generated_at)
    outcome = NodeOutcome(state="unknown", reasons=[], effective=effective, finding=finding, active_placement_ids=[])
    return _intent_effect_summary_diff_from_record(build_node_report_record(node_input, outcome))


def _active_placement_not_applied_diff(entry: dict) -> DiffRecord:
    target = Target(kind="node", slug=entry["desired_node_slug"], name=entry["desired_node"], id=entry["desired_node_id"])
    placement = entry["placement"]
    return DiffRecord(
        target=target,
        code=ACTIVE_PLACEMENT_NOT_APPLIED,
        severity=Severity.WARNING,
        message=(
            f"{entry['desired_node_slug']}: placement {placement['instance_name']!r} is recorded as active but "
            f"not applied because node lifecycle {entry['node_lifecycle']!r} is outside production scope"
        ),
        desired={"placement": placement},
        actual={
            "node_lifecycle": entry["node_lifecycle"],
            "eligible_lifecycles": entry["eligible_lifecycles"],
            "application_status": "not_applied",
        },
        sources=["desired", "actual"],
    )


@register("node")
def node_intent_matching(snapshot: SourceSnapshot, context: DriftContext) -> Iterator[DiffRecord]:
    node_evaluations = evaluate_all_nodes(snapshot)
    for node in snapshot.desired.nodes:
        target = Target(kind="node", slug=node.slug, name=node.name, id=node.id)
        yield from _gap_diffs(target, node_evaluations[node.id])


@register("endpoint")
def endpoint_intent_matching(snapshot: SourceSnapshot, context: DriftContext) -> Iterator[DiffRecord]:
    node_evaluations = evaluate_all_nodes(snapshot)
    endpoint_evaluations = evaluate_all_endpoints(snapshot, node_evaluations)
    nodes_by_id = {node.id: node for node in snapshot.desired.nodes}
    for endpoint in snapshot.desired.endpoints:
        node = nodes_by_id.get(endpoint.node_id)
        target = Target(
            kind="node",
            slug=node.slug if node is not None else None,
            name=node.name if node is not None else None,
            id=endpoint.node_id,
        )
        yield from _gap_diffs(target, endpoint_evaluations[endpoint.id])


@register("service")
def service_intent_matching(snapshot: SourceSnapshot, context: DriftContext) -> Iterator[DiffRecord]:
    if context.profile_reconciliation_error is not None:
        # fix_sshkey3 Step 5 (contract item 1): an unavailable reconciliation
        # contract is a classified global blocker, never a silent
        # convergence -- no managed-file content-drift check runs this round
        # (profile_reconciliation=None below), and this diff alone already
        # blocks every scope (Decision 1: global diffs block all scopes).
        yield DiffRecord(
            target=Target(kind="global"),
            code="deployment_profile_reconciliation_unavailable",
            severity=Severity.ERROR,
            message=context.profile_reconciliation_error,
            sources=["actual"],
        )
    service_evaluations = evaluate_all_services(
        snapshot,
        generated_at=context.generated_at,
        stale_after_hours=context.service_observation_max_age_hours,
        profile_reconciliation=(
            None if context.profile_reconciliation_error is not None else context.profile_reconciliation
        ),
    )
    for service in snapshot.desired.services:
        target = Target(kind="service", slug=service.slug, name=service.name, id=service.id)
        yield from _gap_diffs(target, service_evaluations[service.id])


def _gap_diffs(target: Target, evaluation: EvaluationResult) -> Iterator[DiffRecord]:
    label = target.slug or target.name or target.id or "?"
    for gap in evaluation.gap_summary.get("gaps", []):
        code = gap["code"]
        severity = _SEVERITY_BY_GAP_SEVERITY.get(gap.get("severity"), Severity.WARNING)
        desired = {"expected": gap["expected"]} if "expected" in gap else {}
        actual = {"actual": gap["actual"]} if "actual" in gap else {}
        yield DiffRecord(
            target=target,
            code=code,
            severity=severity,
            message=f"{label}: {code}",
            desired=desired,
            actual=actual,
            sources=["desired", "actual"],
        )
