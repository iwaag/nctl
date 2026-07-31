"""Shared result contracts and common facts for drift evaluators.

Resource-specific node, endpoint, and service evaluators live in their own
modules.  This module intentionally owns only their shared result shape,
status vocabulary, and small value/reference helpers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from ipaddress import ip_interface
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nctl_core.sources.actual import ActualIPAddress
    from nctl_core.sources.desired import DesiredService

NODE_TARGET_TYPE = "desired_node"
ENDPOINT_TARGET_TYPE = "desired_endpoint"
SERVICE_TARGET_TYPE = "desired_service"

NO_DATA_GAP_CODES = frozenset(
    {
        "missing_actual_node",
        "missing_service_lifecycle",
        "service_observation_missing",
        "service_observation_stale",
    }
)


@dataclass(frozen=True)
class EvaluationResult:
    """The computed-fresh equivalent of a persisted `IntentEvaluation` row."""

    target_type: str
    target_id: str
    status: str
    deterministic_summary: dict[str, Any]
    actual_refs: list[dict[str, Any]]
    observed_facts: dict[str, Any]
    expected_facts: dict[str, Any]
    gap_summary: dict[str, Any]
    recommended_actions: list[dict[str, Any]] = field(default_factory=list)

    def as_row(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "observed_facts": self.observed_facts,
            "deterministic_summary": self.deterministic_summary,
            "actual_refs": self.actual_refs,
        }


def _expected_service_facts(desired_service: DesiredService) -> dict[str, Any]:
    return {
        "name": _text(desired_service.name),
        "slug": _text(desired_service.slug),
        "lifecycle": _text(desired_service.lifecycle),
    }


def _actual_ref(object_type: str, obj: Any) -> dict[str, Any]:
    name = getattr(obj, "name", None)
    if not name and object_type == "ipam.ipaddress":
        name = _ip_address_display(obj)
    return {"object_type": object_type, "id": getattr(obj, "id", ""), "name": _text(name)}


def _target_ref(obj_id: str, name: str | None) -> dict[str, Any]:
    return {"id": obj_id, "name": _text(name)}


def _host_address(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    try:
        return str(ip_interface(text).ip)
    except ValueError:
        return text.split("/", maxsplit=1)[0]


def _ip_address_display(actual_ip: ActualIPAddress) -> str:
    host = _text(actual_ip.host)
    mask_length = actual_ip.mask_length
    return f"{host}/{mask_length}" if host and mask_length is not None else host


def _first_text(*values: Any) -> str:
    for value in values:
        text = _text(value)
        if text:
            return text
    return ""


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _norm(value: Any) -> str:
    return _text(value).lower()
