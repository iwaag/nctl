"""Bounded post-convergence cleanup of one retired LXC's ledger records."""
from __future__ import annotations

import hashlib
from typing import Any

import yaml

from pydantic import BaseModel, Field

from nctl_core.artifacts import ArtifactError, OperationArtifacts
from nctl_core.artifacts import atomic_write_private
from nctl_core.config import Config, ConfigError
from nctl_core.desired_write import DesiredWriteError, submit_batch
from nctl_core.drift_render import fetch_and_compute_drift
from nctl_core.events import OperationLog
from nctl_core.nautobot import NautobotClient, NautobotError
from nctl_core.output import Envelope, EnvelopeError

PRUNE_SCHEMA = "nctl.prune.v1"
ACTUAL_PRUNE_PATH = "/api/plugins/intent-catalog/retirement-prune/actual/"


class PruneData(BaseModel):
    operation_id: str
    mode: str
    host: str
    event_log_path: str
    artifact_dir: str | None = None
    state: str = "failed"
    eligibility: dict[str, Any] = Field(default_factory=dict)
    actual_plan: dict[str, Any] = Field(default_factory=dict)
    desired_operations: list[dict[str, Any]] = Field(default_factory=list)
    completed_steps: list[str] = Field(default_factory=list)


def _error(code: str, message: str, **detail: Any) -> EnvelopeError:
    return EnvelopeError(code=code, message=message, detail=detail)


def _desired_operations(snapshot, node, instance) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for override in snapshot.desired.operational_overrides:
        if override.node_id == node.id:
            operations.append({"op": "delete", "kind": "desired_node_operational_override", "key": {"desired_node": node.slug}, "values": {}})
    for endpoint in snapshot.desired.endpoints:
        if endpoint.node_id == node.id:
            operations.append({"op": "delete", "kind": "desired_endpoint", "key": {"desired_node": node.slug, "name": endpoint.name, "endpoint_type": endpoint.endpoint_type}, "values": {}})
    operations += [
        {"op": "delete", "kind": "desired_compute_instance", "key": {"desired_node": node.slug}, "values": {}},
        {"op": "delete", "kind": "desired_node", "key": {"slug": node.slug}, "values": {}},
    ]
    return operations


def _remove_operator_input(cfg: Config, host: str, artifacts: OperationArtifacts) -> dict[str, Any]:
    """Remove only this host's upsert rows from the ignored operator input.

    The database has already committed at this point.  Failure to update this
    convenience input is therefore recorded for a retry instead of pretending
    that the deletion was rolled back.
    """
    path = cfg.source_path.parent / ".local" / "desired-state.yaml"
    if not path.exists():
        return {"path": str(path), "changed": False, "reason": "operator input does not exist"}
    raw = path.read_bytes()
    document = yaml.safe_load(raw) or {}
    operations = document.get("operations") if isinstance(document, dict) else None
    if not isinstance(operations, list):
        raise ValueError("operator desired-state input has no operations list")
    def belongs(operation: Any) -> bool:
        if not isinstance(operation, dict) or operation.get("op") != "upsert":
            return False
        key = operation.get("key") or {}
        return (operation.get("kind") == "desired_node" and key.get("slug") == host) or key.get("desired_node") == host
    kept = [operation for operation in operations if not belongs(operation)]
    if len(kept) == len(operations):
        return {"path": str(path), "changed": False, "reason": "host was absent from operator input"}
    document["operations"] = kept
    serialized = yaml.safe_dump(document, sort_keys=False, allow_unicode=True).encode("utf-8")
    artifacts.write_json("operator-input-update.json", {"path": str(path), "before_sha256": hashlib.sha256(raw).hexdigest(), "after_sha256": hashlib.sha256(serialized).hexdigest()})
    atomic_write_private(path, serialized)
    return {"path": str(path), "changed": True}


def _resolve(snapshot, drift, host: str) -> tuple[dict[str, Any], Any, Any, dict[str, Any] | None]:
    nodes = [node for node in snapshot.desired.nodes if node.slug == host]
    if not nodes:
        return {"result": "already_pruned", "reason": "no DesiredNode remains for this slug"}, None, None, None
    if len(nodes) != 1:
        return {"result": "ineligible", "reason": "slug is ambiguous"}, None, None, None
    node = nodes[0]
    instances = [item for item in snapshot.desired.compute_instances if item.desired_node_id == node.id]
    if node.lifecycle != "retired" or len(instances) != 1 or instances[0].desired_presence != "absent":
        return {"result": "ineligible", "reason": "requires one retired DesiredNode with one absent compute instance"}, None, None, None
    instance = instances[0]
    vm_id, device_id = instance.realized_vm_id, node.realized_device_id
    vm = next((item for item in snapshot.actual.virtual_machines if item.id == vm_id), None)
    device = next((item for item in snapshot.actual.devices if item.id == device_id), None)
    if not vm or not device:
        # A failed run can have completed Actual deletion before its Desired
        # batch.  That is a safe, narrow retry state: only the same tombstones
        # may remain, and no new Actual search is permitted.
        return {"result": "actual_already_pruned", "reason": "Actual roots are already gone; only Desired cleanup remains", "desired_node_id": node.id, "compute_instance_id": instance.id}, node, instance, None
    if vm.proxmox is None or vm.proxmox.presence != "absent" or vm.proxmox.guest_type != "lxc":
        return {"result": "ineligible", "reason": "linked Proxmox LXC is not confirmed absent"}, None, None, None
    cluster = next((item for item in snapshot.actual.clusters if item.id == vm.cluster_id), None)
    if not cluster or not cluster.proxmox or cluster.proxmox.observation_state != "complete":
        return {"result": "ineligible", "reason": "Proxmox observation is not complete"}, None, None, None
    status = next((item for item in drift.targets if item.target.kind == "compute_instance" and item.target.id == instance.id), None)
    codes = {item.code for item in status.diffs} if status else set()
    if "compute_instance_removal_complete" not in codes or "compute_instance_destroy_required" in codes:
        return {"result": "ineligible", "reason": "drift does not confirm completed removal", "drift_codes": sorted(codes)}, None, None, None
    eligibility = {"result": "eligible", "desired_node_id": node.id, "compute_instance_id": instance.id,
                   "device_id": device.id, "virtual_machine_id": vm.id, "drift_codes": sorted(codes)}
    return eligibility, node, instance, {"desired_node_id": node.id, "device_id": device.id, "virtual_machine_id": vm.id}


def run_prune(cfg: Config, host: str, *, apply_changes: bool = False, operation_id: str | None = None) -> Envelope[PruneData]:
    op = OperationLog("prune", cfg.events.resolved_log_dir(), operation_id=operation_id)
    op.emit("started", "retirement prune started", host=host, mode="apply" if apply_changes else "plan")
    data = PruneData(operation_id=op.operation_id, mode="apply" if apply_changes else "plan", host=host, event_log_path=str(op.path))
    try:
        artifacts = OperationArtifacts.create(cfg.events.resolved_log_dir(), op.operation_id)
        data.artifact_dir = str(artifacts.root)
    except ArtifactError as exc:
        return _finish(op, data, [_error("artifact_write_failed", str(exc))])
    fetched = fetch_and_compute_drift(cfg)
    if isinstance(fetched, EnvelopeError):
        return _finish(op, data, [fetched])
    snapshot, drift, _generated = fetched
    eligibility, node, instance, payload = _resolve(snapshot, drift, host)
    data.eligibility = eligibility
    artifacts.write_json("eligibility.json", eligibility)
    if eligibility["result"] == "already_pruned":
        data.state = "noop"
        return _finish(op, data, [])
    if eligibility["result"] not in {"eligible", "actual_already_pruned"}:
        return _finish(op, data, [_error("prune_ineligible", eligibility["reason"], eligibility=eligibility)])
    data.desired_operations = _desired_operations(snapshot, node, instance)
    try:
        token = cfg.nautobot.resolve_token()
        with NautobotClient(cfg.nautobot.url, token) as client:
            if payload is not None:
                response = client.rest_post(ACTUAL_PRUNE_PATH, payload)
                if response.status_code != 200:
                    return _finish(op, data, [_error("actual_prune_plan_failed", f"HTTP {response.status_code}", body=response.json())])
                data.actual_plan = response.json()
            else:
                data.actual_plan = {"records": [], "state": "already_deleted"}
            artifacts.write_json("actual-plan.json", data.actual_plan)
            artifacts.write_json("desired-operations.json", data.desired_operations)
            op.emit("plan_created", "retirement prune plan created", actual_records=len(data.actual_plan.get("records", [])), desired_operations=len(data.desired_operations))
            if not apply_changes:
                data.state = "planned"
                return _finish(op, data, [])
            # Re-fetch before mutation: the reviewed roots and drift facts must not have changed.
            refreshed = fetch_and_compute_drift(cfg)
            if isinstance(refreshed, EnvelopeError):
                return _finish(op, data, [refreshed])
            current, current_drift, _ = refreshed
            current_eligibility, _, _, _ = _resolve(current, current_drift, host)
            if current_eligibility != eligibility:
                return _finish(op, data, [_error("prune_target_changed", "target changed since plan", expected=eligibility, actual=current_eligibility)])
            if payload is not None:
                delete_payload = {**payload, "records": data.actual_plan.get("records", [])}
                response = client.rest_delete(ACTUAL_PRUNE_PATH, delete_payload)
                if response.status_code != 200:
                    return _finish(op, data, [_error("actual_prune_failed", f"HTTP {response.status_code}", body=response.json())])
                actual_result = response.json()
                artifacts.write_json("actual-delete-result.json", actual_result)
                data.completed_steps.append("actual_deleted")
                op.emit("actual_deleted", "Actual ledger records deleted", result=actual_result)
            try:
                desired_result = submit_batch(client, data.desired_operations)
            except DesiredWriteError as exc:
                artifacts.write_json("desired-delete-error.json", exc.artifact)
                return _finish(op, data, [_error("desired_prune_failed", str(exc), artifact=exc.artifact)])
            artifacts.write_json("desired-delete-result.json", desired_result)
            data.completed_steps.append("desired_deleted")
            op.emit("desired_deleted", "Desired tombstones deleted", result=desired_result)
            try:
                operator_input = _remove_operator_input(cfg, host, artifacts)
                artifacts.write_json("operator-input-result.json", operator_input)
                data.completed_steps.append("operator_input_updated" if operator_input["changed"] else "operator_input_unchanged")
                op.emit("operator_input_updated", "operator Desired input reconciled", result=operator_input)
            except (OSError, ValueError, yaml.YAMLError) as exc:
                artifacts.write_json("operator-input-error.json", {"error": str(exc)})
                op.emit("operator_input_failed", "Desired database deletion succeeded but local input was not updated", level="error", error=str(exc))
                return _finish(op, data, [_error("operator_input_update_failed", str(exc))])
    except (ConfigError, NautobotError, ValueError) as exc:
        return _finish(op, data, [_error("prune_request_failed", str(exc))])
    data.state = "pruned"
    return _finish(op, data, [])


def _finish(op: OperationLog, data: PruneData, errors: list[EnvelopeError]) -> Envelope[PruneData]:
    ok = not errors
    if errors:
        data.state = "failed"
    op.finish(ok, data.state)
    return Envelope.build(PRUNE_SCHEMA, data, errors)


def render_prune_text(envelope: Envelope[PruneData]) -> str:
    data = envelope.data
    lines = [f"prune {data.host}: {data.state}", f"operation: {data.operation_id}"]
    if data.actual_plan:
        lines.append(f"Actual records: {len(data.actual_plan.get('records', []))}")
    if data.desired_operations:
        lines.append(f"Desired deletes: {len(data.desired_operations)}")
    lines.extend(f"error [{item.code}]: {item.message}" for item in envelope.errors)
    return "\n".join(lines)
