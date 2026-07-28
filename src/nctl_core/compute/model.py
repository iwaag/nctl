from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class DesiredComputePlatform(BaseModel):
    """Mirrors `nintent.nautobot_intent_catalog.models.DesiredComputePlatform` (VM p3 Step 5)."""

    id: str
    name: str
    slug: str
    provider_type: str
    lifecycle: str
    control_node_id: str
    config_schema_version: str
    config: dict[str, Any] = {}
    realized_cluster_id: str | None = None
    realized_cluster_source: str | None = None


class DesiredComputeInstance(BaseModel):
    """Mirrors `nintent.nautobot_intent_catalog.models.DesiredComputeInstance` (VM p3 Step 5)."""

    id: str
    desired_node_id: str
    platform_id: str
    instance_kind: str
    desired_power_state: str = "running"
    vcpus: int
    memory_mb: int
    root_disk_gb: int
    config_schema_version: str
    config: dict[str, Any] = {}
    realized_vm_id: str | None = None
    realized_vm_source: str | None = None


class DesiredSourceIssue(BaseModel):
    """A row-scoped compute-source validation failure (plan.md Section 5.9).

    Never raised out of `fetch_desired_snapshot()`; collected here instead so
    the rest of the snapshot keeps parsing. `scope` is `target` (this row
    only), `platform` (a platform and everything that references it), or
    `global` (ambiguous cross-row identity, e.g. a duplicate slug).
    """

    code: str
    target_kind: str
    target_id: str | None = None
    target_slug_or_name: str | None = None
    severity: str = "error"
    scope: str = "target"
    message: str
    evidence: dict[str, Any] = {}
    blocked_consumers: list[str] = []
