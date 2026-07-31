"""Pure five-state binding evaluation (idea-A §6, service_relation Phase 3).

Kept separate from `service_placement.py` (which calls into this module,
never the reverse) so the five-state precedence stays one small, directly
testable pure function with no other drift-evaluation dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from nctl_core.production.service_dependencies import normalize_endpoint_url

BINDING_STATES = ("unknown", "unbound", "misbound", "unreachable", "satisfied")


@dataclass(frozen=True)
class BindingCheck:
    """One consumer binding this round resolved a desired endpoint for."""

    desired_url: str
    provider_converged: bool


@dataclass(frozen=True)
class BindingEvaluation:
    state: str
    gap_code: str | None
    evidence: dict[str, Any]
    converged: bool


def _age_hours(value: Any, now: datetime) -> float | None:
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (now.astimezone(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds() / 3600)


def evaluate_binding_state(
    *,
    binding_name: str,
    check: BindingCheck,
    configuration_status: str | None,
    configured_endpoint: str | None,
    reachability_status: str | None,
    checked_at: str | None,
    now: datetime,
    stale_after_hours: int,
) -> BindingEvaluation:
    """idea-A §6 precedence, first match wins:

    1. unknown -- no evidence, evidence unreadable, or stale.
    2. unbound -- `configuration_status: absent`.
    3. misbound -- normalized configured endpoint != normalized desired endpoint.
    4. unreachable -- endpoints match, probe failed.
    5. satisfied -- endpoints match, probe succeeded, evidence fresh; still
       not converged if the provider placement itself isn't converged
       (`binding_provider_not_converged`, per the roadmap's "desired
       resolution succeeded, state satisfied, provider placement converged").
    """

    age = _age_hours(checked_at, now)
    evidence: dict[str, Any] = {
        "binding_name": binding_name,
        "desired_endpoint": check.desired_url,
        "configured_endpoint": configured_endpoint,
        "configuration_status": configuration_status,
        "reachability_status": reachability_status,
        "checked_at": checked_at,
        "age_hours": age,
        "stale_after_hours": stale_after_hours,
    }

    if configuration_status is None or configuration_status == "unreadable" or age is None or age > stale_after_hours:
        return BindingEvaluation("unknown", "binding_unknown", evidence, converged=False)
    if configuration_status == "absent":
        return BindingEvaluation("unbound", "binding_unbound", evidence, converged=False)

    if normalize_endpoint_url(configured_endpoint or "") != normalize_endpoint_url(check.desired_url):
        return BindingEvaluation("misbound", "binding_misbound", evidence, converged=False)

    if reachability_status != "reachable":
        return BindingEvaluation("unreachable", "binding_unreachable", evidence, converged=False)

    if not check.provider_converged:
        return BindingEvaluation("satisfied", "binding_provider_not_converged", evidence, converged=False)

    return BindingEvaluation("satisfied", None, evidence, converged=True)
