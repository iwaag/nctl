"""Initial reconcilers (Phase 4 Step 5).

Each registered `Reconciler` is metadata (id/action_kind/mutates/
requires_observation); the `plan_*` functions here are the per-target logic
that turns a diff group into a `ReconcileAction`, or declines with a
`Fallback` the planner folds into `manual_review`/`unsupported`. Execution
(PATCH/Job/Ansible calls) is Steps 6-7; nothing here mutates anything.

- `observe_node` -- one plan-wide action batching every target with an
  evidence-gap diff (Decision: fresh nodeutils collection/ingest may resolve
  or refine these; see `classify.py`'s `_OBSERVATION_CODES`).
- `link_actual_node` -- the unique `actual_node_not_linked` case (Decision 5).
  Re-derives the candidate from typed snapshot evidence
  (`evaluation_snapshot.evaluate_all_nodes`) rather than trusting the diff's
  message text, per Decision 2 ("an executor never improvises from prose").
- `reconcile_ipam` -- triggers the retained IPAM Job scoped to one node
  (Decision 5). Per-instance Job eligibility is Step 6 work.
- `service_profile` / `dnsmasq_config` -- share one code path
  (`plan_service_profile`): which reconciler id ends up on the built action
  depends on the resolved deployment profile's declared action kind
  (Decision 7), since a playbook run and the built-in dnsmasq render/deploy
  are different execution paths in Step 7.
- `new_node_baseline` -- registered for identity/lookup only. It has no
  claimed diff codes: Step 7's executor triggers it procedurally when
  `link_actual_node` has just linked a node newly realized during the same
  operation, not from a drift diff (p4/plan.md Step 5: "a bootstrap action,
  not a permanent drift assertion").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Union

from nctl_core.drift.evaluation_snapshot import evaluate_all_nodes
from nctl_core.drift.model import Target
from nctl_core.sources.snapshot import SourceSnapshot

from .model import Classification, ReconcileAction
from .profiles import ProfileReconciliation
from .registry import Reconciler, register_reconciler

OBSERVE_NODE = register_reconciler(
    Reconciler(id="observe_node", action_kind="observation", mutates=True, requires_observation=False)
)
LINK_ACTUAL_NODE = register_reconciler(
    Reconciler(id="link_actual_node", action_kind="ledger_patch", mutates=True, requires_observation=False)
)
RECONCILE_IPAM = register_reconciler(
    Reconciler(id="reconcile_ipam", action_kind="job", mutates=True, requires_observation=False)
)
SERVICE_PROFILE = register_reconciler(
    Reconciler(id="service_profile", action_kind="playbook", mutates=True, requires_observation=True)
)
DNSMASQ_CONFIG = register_reconciler(
    # fix_sshkey3 Step 5: requires_observation=True -- a dnsmasq deploy must
    # be followed by a fresh nodeutils collection/ingest so the next round's
    # drift compares against the just-deployed digest, not stale evidence.
    Reconciler(id="dnsmasq_config", action_kind="dnsmasq_config", mutates=True, requires_observation=True)
)
NEW_NODE_BASELINE = register_reconciler(
    Reconciler(id="new_node_baseline", action_kind="playbook", mutates=True, requires_observation=False)
)


@dataclass(frozen=True)
class Fallback:
    """A reconciler declining to automate one specific instance of its code."""

    classification: Classification
    reason: str
    evidence: dict = field(default_factory=dict)


def plan_observe_node(targets: list[Target], claimed_codes: list[str]) -> ReconcileAction:
    return ReconcileAction(
        id="observe_node",
        reconciler_id=OBSERVE_NODE.id,
        action_kind=OBSERVE_NODE.action_kind,
        targets=targets,
        claimed_diff_codes=sorted(set(claimed_codes)),
        reason="Fresh nodeutils collection and ingest may resolve or refine this evidence gap.",
        mutates=OBSERVE_NODE.mutates,
        requires_observation=OBSERVE_NODE.requires_observation,
    )


def plan_link_actual_node(target: Target, snapshot: SourceSnapshot) -> Union[ReconcileAction, Fallback]:
    evaluation = evaluate_all_nodes(snapshot).get(target.id or "")
    if evaluation is None or not evaluation.actual_refs:
        return Fallback(
            Classification.MANUAL_REVIEW,
            "actual_node_not_linked was reported but no unique candidate could be "
            "re-derived from the current snapshot",
        )
    candidate = evaluation.actual_refs[0]
    return ReconcileAction(
        id=f"link_actual_node:{target.slug}",
        reconciler_id=LINK_ACTUAL_NODE.id,
        action_kind=LINK_ACTUAL_NODE.action_kind,
        targets=[target],
        claimed_diff_codes=["actual_node_not_linked"],
        reason="A single deterministic actual node candidate was found but is not explicitly linked.",
        evidence={"candidate": candidate},
        mutates=LINK_ACTUAL_NODE.mutates,
        requires_observation=LINK_ACTUAL_NODE.requires_observation,
        parameters={"candidate": candidate},
    )


def plan_reconcile_ipam(target: Target, claimed_codes: list[str]) -> ReconcileAction:
    return ReconcileAction(
        id=f"reconcile_ipam:{target.slug}",
        reconciler_id=RECONCILE_IPAM.id,
        action_kind=RECONCILE_IPAM.action_kind,
        targets=[target],
        claimed_diff_codes=sorted(set(claimed_codes)),
        reason="Trigger the retained Reconcile Desired IPAM Intent Job scoped to this node.",
        mutates=RECONCILE_IPAM.mutates,
        requires_observation=RECONCILE_IPAM.requires_observation,
        parameters={"desired_node_slug": target.slug},
    )


def plan_service_profile(
    target: Target,
    claimed_codes: list[str],
    *,
    deployment_profile: str,
    host_slugs: list[str],
    reconciliation: dict[str, ProfileReconciliation],
) -> Union[ReconcileAction, Fallback]:
    entry = reconciliation.get(deployment_profile)
    if entry is None:
        return Fallback(
            Classification.UNSUPPORTED,
            f"deployment profile {deployment_profile!r} declares no reconciliation metadata",
        )
    if entry.observe_only:
        return Fallback(
            Classification.UNSUPPORTED,
            f"deployment profile {deployment_profile!r} is observe_only; no actuation is available",
        )
    action = entry.action
    if action is None:  # pragma: no cover - ProfileReconciliation guarantees action xor observe_only
        raise AssertionError("ProfileReconciliation with neither action nor observe_only")
    reconciler = DNSMASQ_CONFIG if action.kind == "dnsmasq_config" else SERVICE_PROFILE
    parameters: dict = {"deployment_profile": deployment_profile, "host_slugs": sorted(host_slugs)}
    if action.kind == "playbook":
        parameters["playbook"] = action.playbook
        parameters["playbook_by_os"] = dict(action.playbook_by_os)
    return ReconcileAction(
        id=f"{reconciler.id}:{deployment_profile}:{target.slug}",
        reconciler_id=reconciler.id,
        action_kind=reconciler.action_kind,
        targets=[target],
        claimed_diff_codes=sorted(set(claimed_codes)),
        reason=f"Run the {deployment_profile!r} profile's reconciliation action for this service.",
        mutates=reconciler.mutates,
        requires_observation=reconciler.requires_observation,
        parameters=parameters,
    )
