"""Pure desired-placement versus observed-service evaluation."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

RUNNING_STATES = frozenset({"running", "active"})
_OBSERVED_SYSTEM_MAP = {"Linux": "linux", "Darwin": "macos"}
_MANAGED_FILE_UNREADABLE_STATUSES = frozenset({"unreadable", "too_large"})


@dataclass(frozen=True)
class ContentSpec:
    """One managed-file content-drift check to run for a service's active placements.

    fix_sshkey3 Step 5: process state (`RUNNING_STATES`) and managed-file
    content are two independent actual-state dimensions -- a `ContentSpec`
    is checked regardless of what the process-state check above found, and
    vice versa. `desired_digest` is the same
    `dnsmasq_render.compute_dnsmasq_render(snapshot).content_sha256` value
    for every placement of the service it applies to (there is one desired
    dnsmasq artifact, not one per node), but each placement's *observed*
    digest is still verified independently.
    """

    managed_file_key: str
    desired_digest: str
    # fix_sshkey4 Step 3 (corrected contract 4): the path/algorithm the
    # active `deployment_profile_reconciliation` metadata currently names --
    # a digest match alone is not sufficient if the stored observation was
    # collected under a different (e.g. since-rotated) destination path.
    expected_path: str
    digest_algo: str = "sha256"


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


_MANAGED_FILE_IDENTIFIED_STATUSES = frozenset({"present", "missing"}) | _MANAGED_FILE_UNREADABLE_STATUSES


def _evaluate_content_drift(report: dict[str, Any], entry: dict[str, Any] | None, content_spec: ContentSpec) -> None:
    """Append `service_config_*` gaps to `report` -- independent of the process-state gaps above."""

    report["desired_content_digest"] = content_spec.desired_digest
    report["expected_content_path"] = content_spec.expected_path
    managed_files = entry.get("managed_files") if isinstance(entry, dict) else None
    file_result = (
        managed_files.get(content_spec.managed_file_key) if isinstance(managed_files, dict) else None
    )
    if not isinstance(file_result, dict):
        report["gaps"].append({"code": "service_config_observation_missing"})
        return
    status = file_result.get("status")
    report["observed_content_status"] = status
    observed_path = file_result.get("path")
    report["observed_content_path"] = observed_path

    # fix_sshkey4 Step 3 (corrected contract 4): a present/missing/unreadable
    # result under the expected managed-file key whose *reported path*
    # disagrees with the currently active metadata contract is stale
    # observation identity, not correctness -- classified OBSERVATION so a
    # fresh probe (using the current probe hint) runs before any deployment
    # is planned from it, never a blind deploy.
    if status in _MANAGED_FILE_IDENTIFIED_STATUSES and observed_path != content_spec.expected_path:
        report["gaps"].append({"code": "service_config_observation_mismatch"})
        return

    if status == "missing":
        report["gaps"].append({"code": "service_config_missing"})
    elif status in _MANAGED_FILE_UNREADABLE_STATUSES:
        report["gaps"].append({"code": "service_config_unreadable", "status": status})
    elif status == "present":
        observed_digest = file_result.get("sha256")
        report["observed_content_digest"] = observed_digest
        if observed_digest != content_spec.desired_digest:
            report["gaps"].append({"code": "service_config_mismatch"})
    else:
        report["gaps"].append({"code": "service_config_observation_missing"})


def evaluate_active_placement(
    placement: dict[str, Any],
    observed_key: str,
    devices: dict[str, dict[str, Any]],
    *,
    now: datetime,
    stale_after_hours: int,
    content_spec: ContentSpec | None = None,
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
        "host_os": placement.get("host_os"),
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
    if normalized_os is None:
        report["gaps"].append({"code": "service_observation_missing", "reason": "observed_system_missing"})

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

    if content_spec is not None:
        _evaluate_content_drift(report, entry, content_spec)
    return report


def evaluate_placement_drift(
    services: list[dict[str, Any]],
    placements: list[dict[str, Any]],
    devices: dict[str, dict[str, Any]],
    device_node_map: dict[str, str],
    *,
    now: datetime,
    stale_after_hours: int,
    content_spec_by_service_id: dict[str, ContentSpec] | None = None,
) -> dict[str, Any]:
    placements_by_service: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for placement in placements:
        placements_by_service[str(placement.get("service_id"))].append(placement)
    content_spec_by_service_id = content_spec_by_service_id or {}
    report = {}
    for service in sorted(services, key=lambda item: str(item.get("id"))):
        service_id = str(service.get("id"))
        observed_key = str(service.get("observed_key") or service.get("name"))
        content_spec = content_spec_by_service_id.get(service_id)
        rows = sorted(placements_by_service.get(service_id, []), key=lambda item: (str(item.get("instance_name")), str(item.get("placement_id"))))
        placement_reports = [
            evaluate_active_placement(
                row, observed_key, devices, now=now, stale_after_hours=stale_after_hours, content_spec=content_spec
            )
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
