"""Pure desired-placement versus observed-service evaluation."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

RUNNING_STATES = frozenset({"running", "active"})
_OBSERVED_SYSTEM_MAP = {"Linux": "linux", "Darwin": "macos"}


def normalize_observed_os(value: Any) -> str | None:
    text = str(value or "").strip()
    return _OBSERVED_SYSTEM_MAP.get(text) if text else None


def age_hours(value: Any, now: datetime) -> float | None:
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


def observed_service_entry(device_facts: dict[str, Any], observed_key: str) -> dict[str, Any] | None:
    observed = device_facts.get("observed_services")
    if not isinstance(observed, dict):
        return None
    entry = observed.get(observed_key)
    return entry if isinstance(entry, dict) else None


def evaluate_active_placement(
    placement: dict[str, Any],
    observed_key: str,
    devices: dict[str, dict[str, Any]],
    *,
    now: datetime,
    stale_after_hours: int,
) -> dict[str, Any]:
    device_id = placement.get("realized_device_id")
    policy = placement.get("actual_state_policy")
    report = {
        "placement_id": placement.get("placement_id"),
        "instance_name": placement.get("instance_name"),
        "node_id": placement.get("node_id"),
        "node_slug": placement.get("node_slug"),
        "deployment_profile": placement.get("deployment_profile"),
        "realized_device_id": device_id,
        "actual_state_policy": policy,
        "observed_key": observed_key,
        "observed_state": None,
        "observed_at": None,
        "gaps": [],
    }
    if policy == "declared":
        return report
    facts = devices.get(device_id) if device_id else None
    if facts is None:
        report["gaps"].append({"code": "service_observation_missing", "reason": "no_realized_device" if not device_id else "device_facts_unavailable"})
        return report

    observed_at = facts.get("service_inventory_updated_at")
    report["observed_at"] = observed_at
    age = age_hours(observed_at, now)
    if age is None:
        report["gaps"].append({"code": "service_observation_missing", "reason": "service_inventory_updated_at_missing"})
    elif age > stale_after_hours:
        report["gaps"].append({"code": "service_observation_stale", "age_hours": age})

    normalized_os = normalize_observed_os(facts.get("observed_system"))
    expected_os = placement.get("expected_host_os")
    if normalized_os is None:
        report["gaps"].append({"code": "service_observation_missing", "reason": "observed_system_missing"})
    elif expected_os and normalized_os != expected_os:
        report["gaps"].append({"code": "service_placement_os_mismatch", "expected_host_os": expected_os, "observed_host_os": normalized_os})

    entry = observed_service_entry(facts, observed_key)
    if entry is None:
        report["gaps"].append({"code": "service_missing"})
    else:
        state = str(entry.get("state") or "").lower()
        report["observed_state"] = entry.get("state")
        report["observed_source"] = entry.get("source")
        report["observed_endpoint"] = entry.get("endpoint")
        report["observed_checked_at"] = entry.get("checked_at") or observed_at
        if state not in RUNNING_STATES:
            report["gaps"].append({"code": "service_not_running", "observed_state": entry.get("state")})
    return report


def evaluate_placement_drift(
    services: list[dict[str, Any]],
    placements: list[dict[str, Any]],
    devices: dict[str, dict[str, Any]],
    device_node_map: dict[str, str],
    *,
    now: datetime,
    stale_after_hours: int,
) -> dict[str, Any]:
    placements_by_service: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for placement in placements:
        placements_by_service[str(placement.get("service_id"))].append(placement)
    report = {}
    for service in sorted(services, key=lambda item: str(item.get("id"))):
        service_id = str(service.get("id"))
        observed_key = str(service.get("observed_key") or service.get("name"))
        rows = sorted(placements_by_service.get(service_id, []), key=lambda item: (str(item.get("instance_name")), str(item.get("placement_id"))))
        placement_reports = [
            evaluate_active_placement(row, observed_key, devices, now=now, stale_after_hours=stale_after_hours)
            for row in rows
        ]
        target_devices = {row.get("realized_device_id") for row in rows if row.get("realized_device_id")}
        unexpected = []
        for device_id in sorted(devices):
            if device_id in target_devices:
                continue
            entry = observed_service_entry(devices[device_id], observed_key)
            if entry is None or str(entry.get("state") or "").lower() not in RUNNING_STATES:
                continue
            unexpected.append({
                "code": "service_observed_on_wrong_node",
                "device_id": device_id,
                "node_id": device_node_map.get(device_id),
                "observed_key": observed_key,
                "observed_state": entry.get("state"),
                "observed_source": entry.get("source"),
                "observed_checked_at": entry.get("checked_at") or devices[device_id].get("service_inventory_updated_at"),
            })
        report[service_id] = {
            "observed_key": observed_key,
            "placements": placement_reports,
            "unexpected_locations": unexpected,
            "status": "no_active_placement" if not rows else "drift" if any(row["gaps"] for row in placement_reports) or unexpected else "satisfied",
        }
    return report
