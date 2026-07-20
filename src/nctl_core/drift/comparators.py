"""Initial comparators (Phase 2 Step 3).

- `node_existence` — a lightweight, non-fuzzy existence check: does a desired
  node's `realized_device_id`/`realized_vm_id` actually resolve in the actual
  snapshot, and does a node whose operational config requires actual state
  have *any* realized object at all? This is deliberately not the full
  candidate-matching/ranking nintent's `evaluate_node_intent` does (scoring
  unlinked nodes against Device/VM candidates by name/serial) — that fuzzy
  matching is Step 4's evaluation port. Step 3 only checks the links that
  already exist.
- `ingest_lag` — compares each observed nodeutils dump's `collected_at`
  against the actual-backed device's last-ingested `last_seen` custom field
  (via `sources.actual.ActualFacts.collected_at`). A dump newer than the last
  ingest means nauto's `Ingest Nodeutils Inventory` Job hasn't run since the
  node last reported — `info` severity, since it's expected between
  collection and ingest, not necessarily wrong.
- `production_policy` — reuses the Step 2 composer (`compose_production_inventory`,
  which itself reuses `evaluate_platform_policy` and the skip-reason helpers)
  instead of reimplementing platform-policy/freshness logic a second time, so
  the composer's `skipped`/`drift` report entries and these diffs can never
  disagree by construction. A composition-wide `ContractError` (e.g. a
  placement referencing an unknown deployment profile) becomes one
  `kind="global"` diff rather than failing the whole drift run.
- `node_intent_matching` / `endpoint_intent_matching` / `service_intent_matching`
  (Phase 2 Step 4) — thin wrappers around the ported `evaluation.py` pure
  functions (`evaluate_node_intent`/`evaluate_endpoint_intent`/
  `evaluate_service_intent`, via `evaluation_snapshot.py`'s snapshot adapter):
  each gap the evaluator produces becomes one `DiffRecord` whose `code` is the
  gap's code unchanged and whose severity is derived from the gap's nintent
  severity (`conflict`/`missing`/`unknown` -> `error`, `partial`/
  `needs_review` -> `warning`) — see `_SEVERITY_BY_GAP_SEVERITY`. This
  supersedes `node_existence`'s existence-only check with the real fuzzy
  candidate-ranking nintent's Evaluate Jobs did; `node_existence` stays
  registered too (not removed) because it is a strictly faster, narrower
  check (dangling FK / policy-requires-realization) that a reader might want
  independent of the heavier candidate-scoring pass, and removing it would
  make `realized_device_missing`/`realized_vm_missing`/`no_realized_object`
  indistinguishable from the fuzzy-matching codes in the diff stream.
  `endpoint_intent_matching` and `service_intent_matching` are registered
  under their own resource types (`"endpoint"`/`"service"`) for registry
  bookkeeping only — `Target.kind` for an endpoint gap is still `"node"`
  (attributed to the endpoint's owning desired node, matching the seeded
  per-node targets in `engine.py`) since a desired endpoint has no
  independent drift-status lifecycle of its own in the roadmap's vocabulary.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterator

from nctl_core.production.adapter import build_production_node_inputs
from nctl_core.production.composer import (
    ACTIVE_PLACEMENT_NOT_APPLIED,
    ContractError,
    compose_production_inventory,
    unapplied_placement_findings,
)
from nctl_core.production.derivation import DerivationFailure, resolve_operational_values
from nctl_core.sources.actual import ActualDevice
from nctl_core.sources.snapshot import SourceSnapshot

from .context import DriftContext
from .evaluation import EvaluationResult
from .evaluation_snapshot import evaluate_all_endpoints, evaluate_all_nodes, evaluate_all_services
from .model import DiffRecord, Severity, Target
from .registry import register

_SEVERITY_BY_GAP_SEVERITY = {
    "conflict": Severity.ERROR,
    "missing": Severity.ERROR,
    "unknown": Severity.ERROR,
    "partial": Severity.WARNING,
    "needs_review": Severity.WARNING,
}

# A canonical placeholder generation id/digest: production_policy discards the
# composed inventory/report's generation-specific fields (only `skipped` and
# `drift` are read), so a real UUID/digest would be pure noise here.
_PLACEHOLDER_GENERATION_ID = "00000000-0000-0000-0000-000000000000"
_PLACEHOLDER_DIGEST = "0" * 64


@register("node")
def node_existence(snapshot: SourceSnapshot, context: DriftContext) -> Iterator[DiffRecord]:
    override_by_node = {item.node_id: item for item in snapshot.desired.operational_overrides}
    devices_by_id = {device.id: device for device in snapshot.actual.devices}
    vms_by_id = {vm.id: vm for vm in snapshot.actual.virtual_machines}

    for node in snapshot.desired.nodes:
        target = Target(kind="node", slug=node.slug, name=node.name, id=node.id)
        operational_override = override_by_node.get(node.id)

        if node.realized_device_id and node.realized_device_id not in devices_by_id:
            yield DiffRecord(
                target=target,
                code="realized_device_missing",
                severity=Severity.ERROR,
                message=(
                    f"{node.slug}: references realized_device {node.realized_device_id!r}, "
                    "which no longer exists in Nautobot"
                ),
                desired={"realized_device_id": node.realized_device_id},
                sources=["desired", "actual"],
            )
        if node.realized_vm_id and node.realized_vm_id not in vms_by_id:
            yield DiffRecord(
                target=target,
                code="realized_vm_missing",
                severity=Severity.ERROR,
                message=(
                    f"{node.slug}: references realized_vm {node.realized_vm_id!r}, "
                    "which no longer exists in Nautobot"
                ),
                desired={"realized_vm_id": node.realized_vm_id},
                sources=["desired", "actual"],
            )
        if (
            (operational_override is None or operational_override.declared_host_os is None)
            and not node.realized_device_id
            and not node.realized_vm_id
        ):
            yield DiffRecord(
                target=target,
                code="no_realized_object",
                severity=Severity.ERROR,
                message=f"{node.slug}: observed operation is required but no realized device or VM is linked",
                desired={"actual_state_policy": "required"},
                sources=["desired"],
            )


@register("node")
def ingest_lag(snapshot: SourceSnapshot, context: DriftContext) -> Iterator[DiffRecord]:
    devices_by_name = {device.name: device for device in snapshot.actual.devices}
    node_by_device_id = {node.realized_device_id: node for node in snapshot.desired.nodes if node.realized_device_id}

    for observed in snapshot.observed:
        device = devices_by_name.get(observed.hostname)
        if device is None:
            continue
        node = node_by_device_id.get(device.id)
        target = (
            Target(kind="node", slug=node.slug, name=node.name, id=node.id)
            if node is not None
            else Target(kind="device", name=device.name, id=device.id)
        )

        diff = _ingest_lag_diff(target, observed.hostname, observed.collected_at, device)
        if diff is not None:
            yield diff


def _ingest_lag_diff(target: Target, hostname: str, observed_at: datetime, device: ActualDevice) -> DiffRecord | None:
    facts = device.actual_facts()
    if facts.collected_at is None:
        return DiffRecord(
            target=target,
            code="ingest_lag",
            severity=Severity.INFO,
            message=f"{hostname}: a nodeutils dump exists but Nautobot has never ingested it",
            actual={"nautobot_last_seen": None, "dump_collected_at": observed_at.isoformat()},
            sources=["observed", "actual"],
        )
    try:
        actual_collected_at = datetime.fromisoformat(facts.collected_at)
    except ValueError:
        return None
    if observed_at.tzinfo is None or observed_at <= actual_collected_at:
        return None
    return DiffRecord(
        target=target,
        code="ingest_lag",
        severity=Severity.INFO,
        message=(
            f"{hostname}: nodeutils dump ({observed_at.isoformat()}) is newer than "
            f"Nautobot's last ingest ({facts.collected_at})"
        ),
        actual={"nautobot_last_seen": facts.collected_at, "dump_collected_at": observed_at.isoformat()},
        sources=["observed", "actual"],
    )


@register("node")
def production_policy(snapshot: SourceSnapshot, context: DriftContext) -> Iterator[DiffRecord]:
    node_inputs = build_production_node_inputs(snapshot)

    for node_input in node_inputs:
        yield _derived_value_provenance_diff(node_input, snapshot, context.generated_at)

    if not context.profiles:
        # The composer itself cannot run without a profile map, but the
        # lifecycle gate behind active_placement_not_applied does not depend
        # on profiles at all (Decision 4) -- recorded intent must not
        # disappear merely because this second, profile-dependent
        # diagnostic source is unavailable.
        for entry in unapplied_placement_findings(node_inputs):
            yield _active_placement_not_applied_diff(entry)
        return

    try:
        composition = compose_production_inventory(
            node_inputs,
            context.profiles,
            generation_id=_PLACEHOLDER_GENERATION_ID,
            generated_at=context.generated_at,
            deployment_profile_digest=_PLACEHOLDER_DIGEST,
        )
    except ContractError as exc:
        yield DiffRecord(
            target=Target(kind="global"),
            code=exc.code,
            severity=Severity.ERROR,
            message=str(exc),
            sources=["desired", "actual"],
        )
        return

    # Structured local errors (Phase 1 Group C) take priority: each becomes
    # its own precise node-targeted diff, and the generic skip-reason
    # conversion below is suppressed for the exact (node, code) pairs they
    # already cover so the same failure never surfaces twice.
    structured_error_keys: set[tuple[str, str]] = set()
    for error in composition.report["errors"]:
        target = Target(
            kind="node", slug=error["desired_node_slug"], name=error["desired_node"], id=error["desired_node_id"]
        )
        yield DiffRecord(
            target=target,
            code=error["code"],
            severity=Severity.ERROR,
            message=error["message"],
            desired=dict(error.get("evidence", {})),
            actual={"stage": error["stage"]},
            sources=["desired", "actual"],
        )
        structured_error_keys.add((error["desired_node_slug"], error["code"]))

    for skip in composition.report["skipped"]:
        target = Target(kind="node", slug=skip["desired_node_slug"], name=skip["desired_node"], id=skip["desired_node_id"])
        for reason in skip["reasons"]:
            if (skip["desired_node_slug"], reason) in structured_error_keys:
                continue
            yield DiffRecord(
                target=target,
                code=reason,
                severity=Severity.ERROR,
                message=f"{skip['desired_node_slug']}: production composition skipped this node ({reason})",
                sources=["desired", "actual"],
            )

    for drift_entry in composition.report["drift"]:
        yield _drift_entry_diff(drift_entry)


def _drift_entry_diff(drift_entry: dict) -> DiffRecord:
    """Dispatch one composer `report["drift"]` entry to its DiffRecord shape
    by code. An unrecognized code is a composer/comparator vocabulary defect,
    not a renderable diff -- it must fail loudly here rather than being
    rendered with an unrelated code's message template.
    """

    code = drift_entry["code"]
    if code == ACTIVE_PLACEMENT_NOT_APPLIED:
        return _active_placement_not_applied_diff(drift_entry)
    raise AssertionError(f"production_policy: unhandled composer drift code {code!r}")


def _derived_value_provenance_diff(node_input, snapshot: SourceSnapshot, generated_at: str) -> DiffRecord:
    try:
        effective = resolve_operational_values(
            node_id=node_input.id,
            node_slug=node_input.slug,
            endpoints=node_input.endpoints,
            override=node_input.operational_override,
            realized_type=node_input.realized.realized_type if node_input.realized else None,
            facts=node_input.realized.facts if node_input.realized else None,
            generated_at=generated_at,
        )
        operational = {"values": effective.as_dict(), "finding": None}
    except DerivationFailure as exc:
        operational = {
            "values": None,
            "finding": {"code": exc.code, "field": exc.field, "evidence": dict(exc.evidence)},
        }

    node = next(item for item in snapshot.desired.nodes if item.id == node_input.id)
    persisted = []
    for field_name in ("realized_device", "realized_vm"):
        value = getattr(node, f"{field_name}_id")
        source = getattr(node, f"{field_name}_source")
        if value is not None:
            persisted.append(
                _persisted_value_record(node.id, field_name, value, source, source == "override")
            )
    for endpoint in sorted(
        (item for item in snapshot.desired.endpoints if item.node_id == node.id), key=lambda item: item.id
    ):
        for field_name in ("dns_name", "mdns_name"):
            value = getattr(endpoint, field_name)
            source = getattr(endpoint, f"{field_name}_source")
            if value is not None:
                persisted.append(
                    _persisted_value_record(
                        endpoint.id, field_name, value, source, source in {"intent", "override"}
                    )
                )
        if endpoint.realized_ip_address_id is not None:
            persisted.append(
                _persisted_value_record(
                    endpoint.id,
                    "realized_ip_address",
                    endpoint.realized_ip_address_id,
                    endpoint.realized_ip_address_source,
                    endpoint.realized_ip_address_source == "override",
                )
            )
    return DiffRecord(
        target=Target(kind="node", slug=node.slug, name=node.name, id=node.id),
        code="derived_value_provenance",
        severity=Severity.INFO,
        message=f"{node.slug}: effective derived/default/override value provenance",
        desired={"operational": operational, "persisted_values": persisted},
        sources=["desired", "actual"],
    )


def _persisted_value_record(row_id: str, field_name: str, value, source: str | None, override_won: bool) -> dict:
    return {
        "field": field_name,
        "record": {
            "value": value,
            "source": source,
            "source_reference": {"kind": "desired_field", "id": row_id, "field": field_name},
            "override_won": override_won,
        },
    }


def _active_placement_not_applied_diff(entry: dict) -> DiffRecord:
    target = Target(kind="node", slug=entry["desired_node_slug"], name=entry["desired_node"], id=entry["desired_node_id"])
    placement = entry["placement"]
    return DiffRecord(
        target=target,
        code=ACTIVE_PLACEMENT_NOT_APPLIED,
        severity=Severity.WARNING,
        message=(
            f"{entry['desired_node_slug']}: placement {placement['instance_name']!r} is recorded as active but "
            f"not applied because node lifecycle {entry['node_lifecycle']!r} is outside production scope"
        ),
        desired={"placement": placement},
        actual={
            "node_lifecycle": entry["node_lifecycle"],
            "eligible_lifecycles": entry["eligible_lifecycles"],
            "application_status": "not_applied",
        },
        sources=["desired", "actual"],
    )


@register("node")
def node_intent_matching(snapshot: SourceSnapshot, context: DriftContext) -> Iterator[DiffRecord]:
    node_evaluations = evaluate_all_nodes(snapshot)
    for node in snapshot.desired.nodes:
        target = Target(kind="node", slug=node.slug, name=node.name, id=node.id)
        yield from _gap_diffs(target, node_evaluations[node.id])


@register("endpoint")
def endpoint_intent_matching(snapshot: SourceSnapshot, context: DriftContext) -> Iterator[DiffRecord]:
    node_evaluations = evaluate_all_nodes(snapshot)
    endpoint_evaluations = evaluate_all_endpoints(snapshot, node_evaluations)
    nodes_by_id = {node.id: node for node in snapshot.desired.nodes}
    for endpoint in snapshot.desired.endpoints:
        node = nodes_by_id.get(endpoint.node_id)
        target = Target(
            kind="node",
            slug=node.slug if node is not None else None,
            name=node.name if node is not None else None,
            id=endpoint.node_id,
        )
        yield from _gap_diffs(target, endpoint_evaluations[endpoint.id])


@register("service")
def service_intent_matching(snapshot: SourceSnapshot, context: DriftContext) -> Iterator[DiffRecord]:
    service_evaluations = evaluate_all_services(
        snapshot,
        generated_at=context.generated_at,
        stale_after_hours=context.service_observation_max_age_hours,
    )
    for service in snapshot.desired.services:
        target = Target(kind="service", slug=service.slug, name=service.name, id=service.id)
        yield from _gap_diffs(target, service_evaluations[service.id])


def _gap_diffs(target: Target, evaluation: EvaluationResult) -> Iterator[DiffRecord]:
    label = target.slug or target.name or target.id or "?"
    for gap in evaluation.gap_summary.get("gaps", []):
        code = gap["code"]
        severity = _SEVERITY_BY_GAP_SEVERITY.get(gap.get("severity"), Severity.WARNING)
        desired = {"expected": gap["expected"]} if "expected" in gap else {}
        actual = {"actual": gap["actual"]} if "actual" in gap else {}
        yield DiffRecord(
            target=target,
            code=code,
            severity=severity,
            message=f"{label}: {code}",
            desired=desired,
            actual=actual,
            sources=["desired", "actual"],
        )
