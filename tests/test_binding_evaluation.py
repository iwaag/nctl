from datetime import datetime, timezone

from nctl_core.drift.binding_evaluation import BindingCheck, evaluate_binding_state

_NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
_FRESH = "2026-08-01T11:30:00+00:00"
_STALE = "2026-07-30T00:00:00+00:00"
_CHECK = BindingCheck(desired_url="http://agstudio.home.arpa:11434/v1", provider_converged=True)


def _evaluate(**overrides):
    kwargs = dict(
        binding_name="llm_provider",
        check=_CHECK,
        configuration_status="present",
        configured_endpoint="http://agstudio.home.arpa:11434/v1",
        reachability_status="reachable",
        checked_at=_FRESH,
        now=_NOW,
        stale_after_hours=24,
    )
    kwargs.update(overrides)
    return evaluate_binding_state(**kwargs)


def test_satisfied_and_converged_when_everything_matches_and_provider_converged():
    result = _evaluate()

    assert result.state == "satisfied"
    assert result.gap_code is None
    assert result.converged is True


def test_satisfied_but_not_converged_when_provider_not_converged():
    check = BindingCheck(desired_url=_CHECK.desired_url, provider_converged=False)

    result = _evaluate(check=check)

    assert result.state == "satisfied"
    assert result.gap_code == "binding_provider_not_converged"
    assert result.converged is False


def test_unbound_when_configuration_status_is_absent():
    result = _evaluate(configuration_status="absent", configured_endpoint=None, reachability_status=None)

    assert result.state == "unbound"
    assert result.gap_code == "binding_unbound"
    assert result.converged is False


def test_misbound_when_normalized_endpoints_differ():
    result = _evaluate(configured_endpoint="http://wrong-host.home.arpa:11434/v1")

    assert result.state == "misbound"
    assert result.gap_code == "binding_misbound"
    assert result.converged is False


def test_misbound_takes_precedence_over_unreachable():
    # A mismatched endpoint is misbound even if the (wrong) endpoint also failed to probe.
    result = _evaluate(configured_endpoint="http://wrong-host.home.arpa:11434/v1", reachability_status="unreachable")

    assert result.state == "misbound"


def test_unreachable_when_endpoints_match_but_probe_failed():
    result = _evaluate(reachability_status="unreachable")

    assert result.state == "unreachable"
    assert result.gap_code == "binding_unreachable"
    assert result.converged is False


def test_unknown_when_no_evidence_at_all():
    result = _evaluate(configuration_status=None, configured_endpoint=None, reachability_status=None, checked_at=None)

    assert result.state == "unknown"
    assert result.gap_code == "binding_unknown"
    assert result.converged is False


def test_unknown_when_evidence_unreadable():
    result = _evaluate(configuration_status="unreadable", configured_endpoint=None, reachability_status=None)

    assert result.state == "unknown"
    assert result.gap_code == "binding_unknown"


def test_unknown_when_evidence_is_stale():
    result = _evaluate(checked_at=_STALE)

    assert result.state == "unknown"
    assert result.gap_code == "binding_unknown"


def test_unknown_takes_precedence_over_unbound_when_evidence_missing_entirely():
    # No checked_at at all (never observed) -- not merely "absent" evidence.
    result = _evaluate(configuration_status="absent", configured_endpoint=None, reachability_status=None, checked_at=None)

    assert result.state == "unknown"


def test_freshness_boundary_is_inclusive_at_exactly_the_threshold():
    checked_at = "2026-07-31T12:00:00+00:00"  # exactly 24h before _NOW

    result = _evaluate(checked_at=checked_at, stale_after_hours=24)

    assert result.state == "satisfied"


def test_freshness_boundary_is_stale_just_past_the_threshold():
    checked_at = "2026-07-31T11:59:00+00:00"  # 24h01m before _NOW

    result = _evaluate(checked_at=checked_at, stale_after_hours=24)

    assert result.state == "unknown"


def test_evidence_carries_binding_name_and_both_endpoint_values():
    result = _evaluate()

    assert result.evidence["binding_name"] == "llm_provider"
    assert result.evidence["desired_endpoint"] == _CHECK.desired_url
    assert result.evidence["configured_endpoint"] == "http://agstudio.home.arpa:11434/v1"
    assert result.evidence["stale_after_hours"] == 24
    assert result.evidence["age_hours"] is not None
