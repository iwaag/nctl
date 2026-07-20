"""Pins Decision 2's fail-closed classification guarantee (Phase 4 Step 5).

`_scan_literal_diff_codes` re-derives the diff-code vocabulary directly from
the comparator/evaluator source files (independent of `classify.py`'s own
table) so this test fails if a new literal code is ever added to those
files without a matching classification -- not just if `classify.py`'s
table is edited incorrectly.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from nctl_core.production.composer import PHASE1_LOCAL_CODES
from nctl_core.reconcile.classify import CODE_CLASSIFICATION, UnclassifiedDiffCodeError, classify
from nctl_core.reconcile.model import Classification

_SRC = Path(__file__).resolve().parents[1] / "src" / "nctl_core"
_SCANNED_FILES = [
    _SRC / "drift" / "evaluation.py",
    _SRC / "drift" / "service_placement.py",
    _SRC / "drift" / "evaluation_snapshot.py",
    _SRC / "sources" / "actual.py",
]
_CODE_LITERAL_RE = re.compile(r'"code":\s*"([a-z0-9_]+)"|code="([a-z0-9_]+)"')

# Codes built from an f-string / variable rather than a literal, enumerated
# by hand from the exact source that constructs them (evaluation.py's
# `_node_mismatches` iterates ("serial", "uuid", "platform") into
# f"{key}_mismatch", and `missing_required_facts` iterates
# `REQUIRED_FACT_BY_CONSUMER`'s values into f"missing_{attr}").
_DYNAMIC_CODES = {
    "serial_mismatch",
    "uuid_mismatch",
    "platform_mismatch",
    "missing_observed_system",
    "missing_mac_address",
    "missing_network_interface",
}


def _scan_literal_diff_codes() -> set[str]:
    codes: set[str] = set()
    for path in _SCANNED_FILES:
        text = path.read_text()
        for match in _CODE_LITERAL_RE.finditer(text):
            codes.add(match.group(1) or match.group(2))
    return codes | _DYNAMIC_CODES


def test_every_producible_diff_code_is_classified():
    scanned = _scan_literal_diff_codes()
    assert scanned, "the scan found no codes at all -- the regex or file list is stale"
    unclassified = sorted(code for code in scanned if code not in CODE_CLASSIFICATION)
    assert unclassified == [], f"new diff code(s) with no reconcile classification: {unclassified}"


def test_every_phase1_local_composer_code_is_classified():
    # better_usability p1: the source scan above targets comparator/evaluator
    # files, not production/composer.py, so this imports the composer's own
    # declared code set directly -- the composer, comparator, and classifier
    # are required to share one vocabulary rather than each declaring their
    # own copy (roadmap.md mandatory check 2).
    assert len(PHASE1_LOCAL_CODES) == 16
    for code in PHASE1_LOCAL_CODES:
        result = classify(code, target_kind="node")
        assert result.classification == Classification.MANUAL_REVIEW, code
        assert result.reconciler_id is None, code


def test_unknown_code_raises_instead_of_defaulting():
    with pytest.raises(UnclassifiedDiffCodeError):
        classify("some_future_code_nobody_reviewed_yet", target_kind="node")


def test_global_target_is_always_manual_review_regardless_of_code():
    # A global diff's code is always a production/contract.py ContractError
    # code, never one of the node/service table's entries -- but even an
    # unrelated code must resolve to manual_review for kind="global", per
    # Decision 1.
    result = classify("anything_at_all", target_kind="global")
    assert result.classification == Classification.MANUAL_REVIEW


@pytest.mark.parametrize(
    "code",
    [
        "actual_node_not_linked",
        "missing_actual_ip_address",
        "actual_ip_address_not_linked",
        "service_missing",
        "service_not_running",
    ],
)
def test_automatic_codes_carry_a_reconciler_id(code):
    result = classify(code, target_kind="node" if "actual" in code else "service")
    assert result.classification == Classification.AUTOMATIC
    assert result.reconciler_id


@pytest.mark.parametrize(
    "code",
    ["ingest_lag", "missing_actual_node", "service_observation_missing", "service_observation_stale"],
)
def test_observation_codes_route_to_observe_node(code):
    result = classify(code, target_kind="node")
    assert result.classification == Classification.OBSERVATION
    assert result.reconciler_id == "observe_node"


@pytest.mark.parametrize(
    "code",
    [
        # ambiguous/multiple candidates
        "ambiguous_actual_node_candidates",
        "multiple_realized_links",
        # actual type/hostname/serial/platform conflicts
        "hostname_mismatch",
        "serial_mismatch",
        "uuid_mismatch",
        "platform_mismatch",
        "realized_actual_type_not_accepted",
        "desired_actual_os_mismatch",
        # invalid/ambiguous IP ranges or interfaces
        "ambiguous_interface",
        "missing_interface_candidate",
        "invalid_ip_policy_range",
        "ambiguous_ip_policy_range",
        # unresolved service dependencies / inactive lifecycle
        "unresolved_dependency",
        "service_lifecycle_inactive",
        # unexpected service removal (a service running somewhere it
        # shouldn't -- Out of scope forbids auto-removal, so this stays
        # manual forever, not just "unsupported for now")
        "service_observed_on_wrong_node",
    ],
)
def test_manual_review_table_from_plan_md_step5(code):
    result = classify(code, target_kind="node")
    assert result.classification == Classification.MANUAL_REVIEW
    assert result.reconciler_id is None
