"""Static diff-code -> classification table (Phase 4 Step 5, Decision 2).

Every error-severity diff code `nctl drift` can currently produce (Phase 2/3's
`comparators.py`, `evaluation.py`, and `service_placement.py`) must resolve to
exactly one of `automatic` / `observation` / `manual_review` / `unsupported`.
A code missing from `CODE_CLASSIFICATION` is a bug, not a default: `classify()`
raises `UnclassifiedDiffCodeError` rather than silently treating an unreviewed
new code as safe to skip or as blocking. `tests/test_reconcile_classify.py`
pins this by asserting every code the current comparators/evaluators can
produce is present here.

`Target.kind == "global"` diffs are a special case handled by `classify()`
itself rather than by this table: they are always `manual_review`, because
every current global diff is a `ContractError` code raised by
`production/contract.py`'s composition/validation (Decision 1: "Global
production-contract errors block every scope"). Enumerating each of that
module's ~30 `ContractError` codes individually here would be brittle byte-
matching against an unrelated module's internals for no behavioral gain --
the blanket rule is both simpler and exhaustive by construction, since
`comparators.py:production_policy` is the only place that ever emits a
`kind="global"` diff and it always does so from a caught `ContractError`.
"""

from __future__ import annotations

from dataclasses import dataclass

from nctl_core.production.composer import PHASE1_LOCAL_CODES

from .model import Classification

# observe_node — missing/stale/insufficient node or service evidence, and
# ingest lag (fresh nodeutils collection/ingest may resolve or refine these).
_OBSERVATION_CODES = frozenset(
    {
        "ingest_lag",
        "missing_actual_data",
        "stale_actual_data",
        "invalid_actual_timestamp",
        "missing_actual_node",
        "no_realized_device",
        "missing_observed_system",
        "missing_mac_address",
        "missing_network_interface",
        "service_observation_missing",
        "service_observation_stale",
        # Dead under the current evaluator (evaluate_all_services always
        # passes observed_facts={}, never None) but kept classified rather
        # than silently unreachable, matching this table's fail-closed intent.
        "service_observed_facts_unknown",
    }
)

# link_actual_node — the unique actual_node_not_linked case (Decision 5): the
# evaluator only assigns this code, as opposed to ambiguous_actual_node_
# candidates, when exactly one strong candidate was found, so the code alone
# already implies eligibility. The reconciler still re-derives the candidate
# ref from typed snapshot evidence rather than trusting the diff message.
_LINK_ACTUAL_NODE_CODES = frozenset({"actual_node_not_linked"})

# reconcile_ipam — endpoint/IP linking gaps the retained IPAM Job can resolve
# (Decision 5). Per-instance Job eligibility (does this endpoint qualify for
# the transactional Job at all) is Step 6 work; Step 5 only classifies the
# code as automatic-by-default.
_RECONCILE_IPAM_CODES = frozenset({"missing_actual_ip_address", "actual_ip_address_not_linked"})

# service_profile — missing/not-running service on an active placement.
# Whether a *specific* service target is actually automatable depends on its
# deployment profile's reconciliation metadata (Decision 7), so these codes
# are classified AUTOMATIC by default here, but `reconcilers.py`'s
# `plan_service_profile_action` can downgrade a specific instance to
# `unsupported` (profile declares no action / observe_only) at plan time.
_SERVICE_PROFILE_CODES = frozenset({"service_missing", "service_not_running"})

# Never automatable: ambiguity/conflict (Decision 2's "automation would be
# unsafe"), or destructive/data-quality issues explicitly out of scope for
# automatic correction (see p4/plan.md "Out of scope").
_MANUAL_REVIEW_CODES = frozenset(
    {
        # Node identity conflicts / ambiguity.
        "multiple_realized_links",
        "realized_actual_type_not_accepted",
        "ambiguous_actual_node_candidates",
        "realized_device_missing",
        "realized_vm_missing",
        "no_realized_object",
        "unsupported_actual_type",
        "serial_mismatch",
        "uuid_mismatch",
        "platform_mismatch",
        "hostname_mismatch",
        "desired_actual_os_mismatch",
        # Endpoint/IP ambiguity or policy conflicts.
        "ip_address_mismatch",
        "ambiguous_ip_address_candidates",
        "missing_interface_candidate",
        "ambiguous_interface",
        "invalid_ip_policy_range",
        "missing_ip_policy_range",
        "ambiguous_ip_policy_range",
        "dhcp_reserved_endpoint_in_dynamic_pool",
        "ip_policy_range_mismatch",
        "static_endpoint_in_dhcp_pool",
        # Service lifecycle/dependency/placement issues that need a human
        # decision, not an actuation.
        "service_lifecycle_inactive",
        "missing_service_lifecycle",
        "unresolved_dependency",
        "service_placement_os_mismatch",
        "service_has_no_active_placement",
        "service_observed_on_wrong_node",
    }
)


@dataclass(frozen=True)
class CodeClassification:
    classification: Classification
    reconciler_id: str | None = None


_TABLE: dict[str, CodeClassification] = {}
_TABLE.update(
    {code: CodeClassification(Classification.OBSERVATION, "observe_node") for code in _OBSERVATION_CODES}
)
_TABLE.update(
    {code: CodeClassification(Classification.AUTOMATIC, "link_actual_node") for code in _LINK_ACTUAL_NODE_CODES}
)
_TABLE.update(
    {code: CodeClassification(Classification.AUTOMATIC, "reconcile_ipam") for code in _RECONCILE_IPAM_CODES}
)
_TABLE.update(
    {code: CodeClassification(Classification.AUTOMATIC, "service_profile") for code in _SERVICE_PROFILE_CODES}
)
_TABLE.update({code: CodeClassification(Classification.MANUAL_REVIEW) for code in _MANUAL_REVIEW_CODES})
# better_usability Phase 1: every target-local production-composition failure
# (`production/composer.py`'s Group C codes) plus `active_placement_not_applied`
# is manual review with no reconciler -- a human must fix the node/placement
# data (or, once Phase 2 ships, several of these codes disappear because the
# field they concern is no longer required). Imported from composer.py rather
# than redeclared so composer, comparator, and classifier can never disagree
# on this vocabulary (roadmap.md's mandatory check 2).
_TABLE.update({code: CodeClassification(Classification.MANUAL_REVIEW) for code in PHASE1_LOCAL_CODES})

CODE_CLASSIFICATION: dict[str, CodeClassification] = dict(_TABLE)


class UnclassifiedDiffCodeError(Exception):
    """A diff code has no entry in `CODE_CLASSIFICATION` (Decision 2's guard rail)."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(
            f"diff code {code!r} has no reconcile classification; add it to "
            "nctl_core.reconcile.classify before it can appear in a plan"
        )


_GLOBAL_CLASSIFICATION = CodeClassification(Classification.MANUAL_REVIEW)


def classify(code: str, *, target_kind: str) -> CodeClassification:
    """Classify one diff's code, given its target kind.

    Raises `UnclassifiedDiffCodeError` for any node/service/endpoint-level
    code not in `CODE_CLASSIFICATION` -- this is the fail-closed guard the
    roadmap requires: a new comparator/evaluator code must be reviewed and
    added here before `nctl reconcile` can plan around it.
    """

    if target_kind == "global":
        return _GLOBAL_CLASSIFICATION
    try:
        return CODE_CLASSIFICATION[code]
    except KeyError:
        raise UnclassifiedDiffCodeError(code) from None
