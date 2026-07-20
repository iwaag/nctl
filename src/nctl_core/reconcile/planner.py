"""The deterministic reconcile planner (Phase 4 Step 5, Decision 2).

`build_plan` never mutates anything: it classifies every selected diff,
asks the owning reconciler to (re-)derive a `ReconcileAction` from typed
snapshot evidence, and otherwise records a `manual_review`/`unsupported`
entry. Registration/iteration order never affects the result -- targets are
always processed in sorted order, matching `drift/registry.py`'s ordering
guarantee for the same reason (the caller must be able to diff two plans
byte-for-byte).
"""

from __future__ import annotations

from datetime import datetime, timezone

from nctl_core.drift.model import DiffRecord, Severity, Target
from nctl_core.production.composer import PHASE1_LOCAL_CODES
from nctl_core.sources.snapshot import SourceSnapshot

from .classify import CODE_CLASSIFICATION, Classification, classify
from .fingerprint import compute_drift_fingerprint
from .model import ManualReviewRecord, PlanScope, ReconcileAction, ReconcilePlan, UnsupportedRecord
from .profiles import ProfileReconciliation
from .reconcilers import Fallback, plan_link_actual_node, plan_observe_node, plan_reconcile_ipam, plan_service_profile
from .registry import topological_order


class HostScopeError(Exception):
    """`scope.host_slug` matched zero or multiple desired nodes."""


def resolve_host_node(snapshot: SourceSnapshot, host_slug: str):
    matches = [node for node in snapshot.desired.nodes if node.slug == host_slug]
    if not matches:
        raise HostScopeError(f"no desired node with slug {host_slug!r}")
    if len(matches) > 1:
        raise HostScopeError(f"multiple desired nodes with slug {host_slug!r}")
    return matches[0]


def select_scoped_diffs(diffs: list[DiffRecord], scope: PlanScope, snapshot: SourceSnapshot) -> list[DiffRecord]:
    """Project the full-cluster diff list onto one scope (Decision 1).

    Global diffs always pass through regardless of scope: "Global production-
    contract errors block every scope because reconcile atomically
    regenerates the full canonical production inventory."
    """

    if scope.kind == "cluster":
        return list(diffs)

    host_node = resolve_host_node(snapshot, scope.host_slug or "")
    service_ids_on_host = {
        placement.service_id
        for placement in snapshot.desired.placements
        if placement.node_id == host_node.id and placement.desired_state == "active"
    }
    services_by_slug = {service.slug: service for service in snapshot.desired.services}

    selected: list[DiffRecord] = []
    for diff in diffs:
        if diff.target.kind == "global":
            selected.append(diff)
        elif diff.target.kind == "node" and diff.target.slug == host_node.slug:
            selected.append(diff)
        elif diff.target.kind == "service":
            service = services_by_slug.get(diff.target.slug or "")
            if service is not None and service.id in service_ids_on_host:
                selected.append(diff)
    return selected


def _target_key(target: Target) -> tuple[str, str]:
    return (target.kind, target.slug or target.name or target.id or "")


def _service_profile_inputs(
    target: Target, snapshot: SourceSnapshot
) -> tuple[str, list[str]] | Fallback:
    service = next((s for s in snapshot.desired.services if s.slug == target.slug), None)
    if service is None:
        return Fallback(Classification.MANUAL_REVIEW, f"no desired service found for slug {target.slug!r}")
    nodes_by_id = {node.id: node for node in snapshot.desired.nodes}
    active = [
        placement
        for placement in snapshot.desired.placements
        if placement.service_id == service.id and placement.desired_state == "active"
    ]
    if not active:
        return Fallback(Classification.MANUAL_REVIEW, f"service {target.slug!r} has no active placement")
    profiles = {placement.deployment_profile for placement in active}
    if len(profiles) > 1:
        return Fallback(
            Classification.MANUAL_REVIEW,
            f"service {target.slug!r} has active placements using different deployment profiles: "
            f"{', '.join(sorted(profiles))}",
        )
    host_slugs = sorted(
        {
            nodes_by_id[placement.node_id].slug
            for placement in active
            if placement.node_id in nodes_by_id
        }
    )
    return next(iter(profiles)), host_slugs


def build_plan(
    *,
    snapshot: SourceSnapshot,
    diffs: list[DiffRecord],
    scope: PlanScope,
    drift_generated_at: str | None,
    profile_reconciliation: dict[str, ProfileReconciliation],
    now: datetime | None = None,
) -> ReconcilePlan:
    scoped_diffs = select_scoped_diffs(diffs, scope, snapshot)
    fingerprint = compute_drift_fingerprint(scoped_diffs)

    manual_review: list[ManualReviewRecord] = []
    unsupported: list[UnsupportedRecord] = []
    observe_targets: dict[tuple[str, str], Target] = {}
    observe_codes: set[str] = set()
    automatic_groups: dict[str, dict[tuple[str, str], tuple[Target, list[str], list[DiffRecord]]]] = {}

    for diff in scoped_diffs:
        if diff.severity != Severity.ERROR and diff.target.kind != "global" and diff.code not in CODE_CLASSIFICATION:
            # A non-error diagnostic in a code nobody has reviewed for
            # reconcile purposes yet is left out of the plan rather than
            # failing the whole operation -- only error diffs are subject
            # to Decision 2's fail-closed classification guarantee.
            continue
        code_classification = classify(diff.code, target_kind=diff.target.kind)
        if code_classification.classification == Classification.OBSERVATION:
            target = diff.target
            if target.kind == "service":
                expected = diff.desired.get("expected", {})
                node_slug = expected.get("node_slug")
                if not node_slug:
                    raise ValueError(
                        f"planner defect: OBSERVATION diff {diff.code!r} on service target "
                        f"{target.slug!r} has no desired.expected.node_slug to resolve to a node"
                    )
                target = Target(kind="node", slug=node_slug, id=expected.get("node_id"))
            key = _target_key(target)
            observe_targets[key] = target
            observe_codes.add(diff.code)
        elif code_classification.classification == Classification.MANUAL_REVIEW:
            manual_review.append(_manual_record(diff, "not automatable: ambiguity, conflict, or destructive change"))
        elif code_classification.classification == Classification.AUTOMATIC:
            reconciler_id = code_classification.reconciler_id or ""
            per_target = automatic_groups.setdefault(reconciler_id, {})
            key = _target_key(diff.target)
            if key not in per_target:
                per_target[key] = (diff.target, [], [])
            per_target[key][1].append(diff.code)
            per_target[key][2].append(diff)
        else:  # pragma: no cover - classify() never returns UNSUPPORTED statically today
            unsupported.append(_unsupported_record(diff, "no reconciler is registered for this code"))

    # Phase 1 (better_usability): every node-targeted Phase 1 local code is a
    # production-actuation blocker for its owning node, derived from this
    # scope's own manual_review records (already target-local by construction
    # -- `nctl reconcile HOST` naturally selects only that host's blocker,
    # cluster scope sees the complete set). A blocked node's manual-review
    # record stays in `manual_review` regardless; this set only prunes
    # service/dnsmasq action host lists below so unrelated healthy hosts are
    # never suppressed by one node's local finding (Decision 5).
    blocked_node_slugs = {
        record.target.slug
        for record in manual_review
        if record.target.kind == "node" and record.code in PHASE1_LOCAL_CODES and record.target.slug
    }

    actions: list[ReconcileAction] = []
    node_targets_by_slug: dict[str, str] = {}  # slug -> link_actual_node action id, for dependency wiring

    if observe_targets:
        ordered_targets = [observe_targets[key] for key in sorted(observe_targets)]
        actions.append(plan_observe_node(ordered_targets, sorted(observe_codes)))

    for key, (target, codes, group_diffs) in sorted(automatic_groups.get("link_actual_node", {}).items()):
        outcome = plan_link_actual_node(target, snapshot)
        if isinstance(outcome, Fallback):
            _apply_fallback(outcome, group_diffs, manual_review, unsupported)
        else:
            actions.append(outcome)
            if target.slug:
                node_targets_by_slug[target.slug] = outcome.id

    for key, (target, codes, group_diffs) in sorted(automatic_groups.get("reconcile_ipam", {}).items()):
        action = plan_reconcile_ipam(target, codes)
        if target.slug and target.slug in node_targets_by_slug:
            action = action.model_copy(update={"dependencies": [node_targets_by_slug[target.slug]]})
        actions.append(action)

    profile_actions_by_profile: dict[str, list[ReconcileAction]] = {}
    for key, (target, codes, group_diffs) in sorted(automatic_groups.get("service_profile", {}).items()):
        inputs = _service_profile_inputs(target, snapshot)
        if isinstance(inputs, Fallback):
            _apply_fallback(inputs, group_diffs, manual_review, unsupported)
            continue
        deployment_profile, host_slugs = inputs
        host_slugs = [slug for slug in host_slugs if slug not in blocked_node_slugs]
        if not host_slugs:
            # Every host this service is placed on is production-blocked by a
            # Phase 1 local finding; the reason isn't lost, it's already the
            # owning node's manual_review record above (Decision 5.4: never
            # create an empty-host action).
            continue
        outcome = plan_service_profile(
            target,
            codes,
            deployment_profile=deployment_profile,
            host_slugs=host_slugs,
            reconciliation=profile_reconciliation,
        )
        if isinstance(outcome, Fallback):
            _apply_fallback(outcome, group_diffs, manual_review, unsupported)
        else:
            actions.append(outcome)
            profile_actions_by_profile.setdefault(deployment_profile, []).append(outcome)

    _wire_profile_dependencies(profile_actions_by_profile, profile_reconciliation)

    order = topological_order(actions)
    actions_by_id = {action.id: action for action in actions}
    ordered_actions = [actions_by_id[action_id] for action_id in order]

    return ReconcilePlan(
        scope=scope,
        drift_fingerprint=fingerprint,
        drift_generated_at=drift_generated_at,
        generated_at=(now or datetime.now(timezone.utc)),
        actions=ordered_actions,
        manual_review=manual_review,
        unsupported=unsupported,
    )


def _wire_profile_dependencies(
    profile_actions_by_profile: dict[str, list[ReconcileAction]],
    profile_reconciliation: dict[str, ProfileReconciliation],
) -> None:
    """Order service/dnsmasq actions per Decision 7's profile dependency metadata.

    An action depends on another already-planned action for a profile it
    declares a dependency on, but only when their host sets overlap -- a
    profile dependency is about actuation order on shared hosts (Prometheus
    before its node-exporter scrape refresh), not a blanket ordering across
    unrelated machines.
    """

    for profile_name, actions in profile_actions_by_profile.items():
        entry = profile_reconciliation.get(profile_name)
        if entry is None:
            continue
        for dep_profile in entry.dependencies:
            dep_actions = profile_actions_by_profile.get(dep_profile, [])
            if not dep_actions:
                continue
            for action in actions:
                own_hosts = set(action.parameters.get("host_slugs", []))
                deps = [
                    dep_action.id
                    for dep_action in dep_actions
                    if own_hosts & set(dep_action.parameters.get("host_slugs", []))
                ]
                if deps:
                    action.dependencies.extend(dep for dep in deps if dep not in action.dependencies)


def _apply_fallback(
    fallback: Fallback,
    group_diffs: list[DiffRecord],
    manual_review: list[ManualReviewRecord],
    unsupported: list[UnsupportedRecord],
) -> None:
    for diff in group_diffs:
        if fallback.classification == Classification.UNSUPPORTED:
            unsupported.append(_unsupported_record(diff, fallback.reason, fallback.evidence))
        else:
            manual_review.append(_manual_record(diff, fallback.reason, fallback.evidence))


def _manual_record(diff: DiffRecord, reason: str, evidence: dict | None = None) -> ManualReviewRecord:
    return ManualReviewRecord(
        target=diff.target,
        code=diff.code,
        severity=diff.severity.value,
        message=diff.message,
        reason=reason,
        evidence=evidence or {"desired": diff.desired, "actual": diff.actual},
    )


def _unsupported_record(diff: DiffRecord, reason: str, evidence: dict | None = None) -> UnsupportedRecord:
    return UnsupportedRecord(
        target=diff.target,
        code=diff.code,
        severity=diff.severity.value,
        message=diff.message,
        reason=reason,
        evidence=evidence or {"desired": diff.desired, "actual": diff.actual},
    )
