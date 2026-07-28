"""Deterministic desired-service drift evaluation."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from nctl_core.sources.desired import DesiredDependency, DesiredService

from .evaluation import EvaluationResult, SERVICE_TARGET_TYPE, _expected_service_facts, _target_ref
from .gap_status import status_from_gaps


def evaluate_service_intent(desired_service: DesiredService, *, dependencies: Iterable[DesiredDependency] = (),
                            resolved_services_by_id: Mapping[str, DesiredService] | None = None,
                            observed_facts: dict[str, Any] | None = None,
                            ai_review_enabled: bool = False) -> EvaluationResult:
    """Evaluate a service using only supplied desired and observed facts."""
    resolved_services_by_id = resolved_services_by_id or {}
    expected = _expected_service_facts(desired_service, list(dependencies), resolved_services_by_id)
    observed = {"service_observation_status": "provided" if observed_facts is not None else "unknown",
                "service_facts": observed_facts or {}, "ai_review": {"enabled": bool(ai_review_enabled), "executed": False}}
    gaps: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    lifecycle = expected.get("lifecycle")
    if lifecycle in {"deprecated", "retired"}:
        gaps.append({"code": "service_lifecycle_inactive", "severity": "needs_review", "lifecycle": lifecycle})
        actions.append({"action": "review_service_lifecycle", "target": _target_ref(desired_service.id, desired_service.name),
                        "reason": "The desired service lifecycle is inactive.", "requires_review": True})
    elif lifecycle in {"", "unknown"}:
        gaps.append({"code": "missing_service_lifecycle", "severity": "unknown"})
    for dependency in expected["dependencies"]:
        if dependency["resolution_status"] == "unresolved":
            gaps.append({"code": "unresolved_dependency", "severity": "partial", "dependency": dependency})
            actions.append({"action": "resolve_service_dependency", "target": _target_ref(desired_service.id, desired_service.name),
                            "dependency": dependency, "reason": "A desired service dependency is unresolved.", "requires_review": True})
    if observed_facts is None:
        gaps.append({"code": "service_observed_facts_unknown", "severity": "unknown"})
    status = status_from_gaps(gaps)
    summary = {"target": _target_ref(desired_service.id, desired_service.name), "status": status,
               "gap_codes": [gap["code"] for gap in gaps], "dependency_counts": expected["dependency_counts"],
               "requirements_present": bool(expected["requirements"]), "service_observation_status": observed["service_observation_status"],
               "ai_review_ready": True, "ai_review_executed": False, "evaluation_scope": "service_lifecycle_requirements_dependencies"}
    return EvaluationResult(target_type=SERVICE_TARGET_TYPE, target_id=desired_service.id, status=status,
                            deterministic_summary=summary, actual_refs=[], observed_facts=observed,
                            expected_facts=expected, gap_summary={"gaps": gaps}, recommended_actions=actions)
