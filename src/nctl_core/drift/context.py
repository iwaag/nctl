"""Shared context passed to every comparator (Phase 2 Step 3).

Kept separate from `SourceSnapshot` because it carries things that are not
"a source" in the Step 1 sense: `generated_at` is when this drift run started
(not when any source was fetched), `profiles` is the validated
deployment-profiles map the production-policy comparator needs (loading it is
a filesystem read against `[ansible] playbook_dir`, not a Nautobot/dump
source), and `events_dir` is only needed by the `converging` status rule.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nctl_core.reconcile.profiles import ProfileReconciliation


@dataclass(frozen=True)
class DriftContext:
    generated_at: str
    profiles: dict[str, Any] = field(default_factory=dict)
    # Set when `load_deployment_profiles` raised `DeploymentProfilesError` (missing,
    # unparsable, or contract-invalid `vars/deployment_profiles.yml`) -- Phase 4 Decision 3:
    # `production_policy` turns this into a classified global ERROR
    # `deployment_profiles_unavailable` instead of silently composing against `{}`. `None`
    # means profiles loaded fine (possibly legitimately empty).
    profiles_error: str | None = None
    # fix_sshkey3 Step 5 (contract item 1): the validated `deployment_profile_reconciliation`
    # map. `profile_reconciliation_error` set (missing/unparsable/invalid, or unreachable
    # because `profiles_error` was already set) means `service_intent_matching` must emit a
    # classified global error and run no managed-file content-drift check at all this round
    # -- never a silent convergence.
    profile_reconciliation: "dict[str, ProfileReconciliation]" = field(default_factory=dict)
    profile_reconciliation_error: str | None = None
    events_dir: Path | None = None
    service_observation_max_age_hours: int = 24
