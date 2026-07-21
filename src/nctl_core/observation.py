"""Collect, validate, cache, and ingest nodeutils observations for scoped hosts."""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import yaml
from pydantic import BaseModel, Field, ValidationError

from nctl_core.ansible import AnsibleRunResult, AnsibleRunner, CommandRunner
from nctl_core.artifacts import ArtifactError, OperationArtifacts, atomic_write_private
from nctl_core.config import Config
from nctl_core.dumps import DumpError, NodeDump, parse_dump_text
from nctl_core.events import OperationLog
from nctl_core.hosts_intent import export_hosts_intent, render_hosts_intent_yml
from nctl_core.jobs import NautobotJobResult, NautobotJobRunner
from nctl_core.names import canonical_node_name
from nctl_core.nautobot import NautobotClient
from nctl_core.sources.desired import DesiredSnapshot
from nctl_core.sources.actual import ActualSnapshot, fetch_actual_snapshot

INGEST_JOB_NAME = "Ingest Nodeutils Inventory"
INGEST_ARTIFACT_NAME = "nodeutils-ingest-summary.json"
INGEST_SUMMARY_SCHEMA = "nodeutils.ingest.summary.v1"


class HostObservation(BaseModel):
    host: str
    collected: bool = False
    cache_path: str | None = None
    ingest_outcome: str | None = None
    error: str | None = None


class ObservationResult(BaseModel):
    ok: bool
    hosts: list[HostObservation] = Field(default_factory=list)
    collection: AnsibleRunResult
    retrieval: AnsibleRunResult
    job: NautobotJobResult | None = None
    actual: ActualSnapshot | None = None
    error: str | None = None


class IngestSummaryRow(BaseModel):
    source: str
    outcome: str
    error: str | None = None


class IngestSummary(BaseModel):
    schema_version: str
    dry_run: bool
    results: list[IngestSummaryRow]


def render_probe_hints(snapshot: DesiredSnapshot, node_id: str) -> str:
    """Render non-secret probe names from active authoritative placements."""

    service_names = {service.id: service.name for service in snapshot.services}
    names = sorted(
        {
            service_names[placement.service_id]
            for placement in snapshot.placements
            if placement.node_id == node_id
            and placement.desired_state == "active"
            and placement.service_id in service_names
        }
    )
    return yaml.safe_dump(
        {"service_probe_hints": {name: {} for name in names}},
        sort_keys=True,
        default_flow_style=False,
    )


def run_observation(
    cfg: Config,
    snapshot: DesiredSnapshot,
    target_slugs: list[str],
    artifacts: OperationArtifacts,
    operation_log: OperationLog,
    *,
    command_runner: CommandRunner | None = None,
    job_runner: Any | None = None,
    actual_fetcher: Callable[[], ActualSnapshot] | None = None,
    now: datetime | None = None,
) -> ObservationResult:
    """Run the Phase 4 observation pipeline without suppressing per-host failures."""

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    export = export_hosts_intent(snapshot.nodes, snapshot.endpoints)
    eligible = {row["inventory_hostname"]: row for row in export.hosts}
    targets = sorted(set(target_slugs))
    unknown = sorted(set(targets) - set(eligible))
    if not targets or unknown:
        detail = "no target hosts" if not targets else f"hosts are not bootstrap-eligible: {', '.join(unknown)}"
        raise ValueError(detail)

    generated_at = now.isoformat().replace("+00:00", "Z")
    inventory_path = artifacts.write_text(
        "bootstrap/hosts_intent.yml",
        render_hosts_intent_yml(export, generated_at=generated_at),
    )
    node_by_id = {node.id: node for node in snapshot.nodes}
    probe_dir = artifacts.directory("probe-config")
    for host in targets:
        node = node_by_id[eligible[host]["desired_node_id"]]
        artifacts.write_text(f"probe-config/{host}.yaml", render_probe_hints(snapshot, node.id))

    runner = AnsibleRunner(
        cfg.ansible.resolved_playbook_dir(cfg.source_path.parent),
        timeout_seconds=cfg.reconcile.ansible_timeout_seconds,
        artifacts=artifacts,
        command_runner=command_runner,
    )
    # ``inventory_path`` deliberately contains the operation-scoped bootstrap hosts only.
    # It lives under the event artifact directory, so Ansible cannot discover the normal
    # generated inventory's adjacent ``group_vars`` (including vaulted connection/become
    # variables) from that source alone. Keep it first so its fresh host selection remains
    # authoritative, then add the configured inventory as the shared variable source.
    shared_inventory = cfg.ansible.resolved_inventory(cfg.source_path.parent)
    limit = ",".join(targets)
    playbook = cfg.ansible.resolved_playbook_dir(cfg.source_path.parent) / "playbooks/nautobot/run_nodeutils_collect.yml"
    operation_log.emit("collection_started", "nodeutils collection started", hosts=targets)
    collection = runner.run(
        [
            "ansible-playbook", "-i", str(inventory_path), "-i", str(shared_inventory), str(playbook),
            "--limit", limit, "-e", "target_hosts=ssh_hosts",
            "-e", f"nodeutils_probe_config_dir={probe_dir}",
        ],
        mode="collect",
        artifact_stem="ansible/collect",
    )

    slurp_dir = artifacts.directory("slurp")
    retrieval = runner.run(
        [
            "ansible", "-i", str(inventory_path), "-i", str(shared_inventory), "ssh_hosts", "--limit", limit,
            "-m", "ansible.builtin.slurp",
            "-a", f"src={cfg.reconcile.remote_report_path}", "--tree", str(slurp_dir),
        ],
        mode="slurp",
        artifact_stem="ansible/slurp",
    )
    observations = {host: HostObservation(host=host) for host in targets}
    decoded: dict[str, tuple[str, NodeDump]] = {}
    for host in targets:
        try:
            text, dump = _decode_slurp(
                slurp_dir / host,
                host,
                max_bytes=cfg.reconcile.max_report_bytes,
                oldest=now - timedelta(hours=cfg.reconcile.max_report_age_hours),
            )
            decoded[host] = (text, dump)
        except (OSError, ValueError, DumpError) as exc:
            observations[host].error = str(exc)

    identities: dict[str, list[str]] = {}
    for host, (_text, dump) in decoded.items():
        identity = canonical_node_name(getattr(dump.identity, "fqdn", None) or dump.identity.hostname)
        identities.setdefault(identity, []).append(host)
    for identity, hosts in identities.items():
        if not identity or len(hosts) > 1:
            message = f"duplicate or empty canonical identity {identity!r}: {', '.join(hosts)}"
            for host in hosts:
                observations[host].error = message
                decoded.pop(host, None)

    reports = []
    for host, (report_text, _dump) in sorted(decoded.items()):
        try:
            artifacts.write_text(f"reports/{host}.json", report_text)
            cache_path = atomic_write_private(
                cfg.inventory.resolved_dumps_dir() / f"{host}.json",
                report_text.encode("utf-8"),
            )
        except ArtifactError as exc:
            observations[host].error = f"{host}: cannot retain validated report: {exc}"
            continue
        observations[host].collected = True
        observations[host].cache_path = str(cache_path)
        reports.append({"source": host, "text": report_text})

    operation_log.emit(
        "reports_retrieved",
        "nodeutils reports retrieved",
        valid_hosts=sorted(row["source"] for row in reports),
        failed_hosts=sorted(set(targets) - {row["source"] for row in reports}),
    )
    job_result = None
    actual_snapshot = None
    pipeline_error = None
    if reports:
        owned_client = None
        try:
            if job_runner is None:
                owned_client = NautobotClient(cfg.nautobot.url, cfg.nautobot.resolve_token())
                job_runner = NautobotJobRunner(
                    owned_client,
                    poll_interval_seconds=cfg.reconcile.job_poll_interval_seconds,
                    timeout_seconds=cfg.reconcile.job_timeout_seconds,
                    artifacts=artifacts,
                    operation_log=operation_log,
                )
            job_result = job_runner.run(
                INGEST_JOB_NAME,
                {
                    "report_batch": json.dumps({"reports": reports}, sort_keys=True),
                    "policy_file": str(cfg.reconcile.ingest_policy_file),
                    "dry_run": False,
                    "max_report_age_hours": cfg.reconcile.max_report_age_hours,
                    "max_report_bytes": cfg.reconcile.max_report_bytes,
                },
                commit=True,
                artifact_name=INGEST_ARTIFACT_NAME,
                artifact_relative_path="jobs/nodeutils-ingest-summary.json",
            )
            submitted_sources = {row["source"] for row in reports}
            summary = _load_ingest_summary(Path(job_result.artifact_path or ""), submitted_sources)
            for row in summary.results:
                observations[row.source].ingest_outcome = row.outcome
                if row.outcome == "skipped":
                    observations[row.source].error = row.error or "ingest skipped report"
            if actual_fetcher is not None:
                actual_snapshot = actual_fetcher()
            elif owned_client is not None:
                actual_snapshot = fetch_actual_snapshot(owned_client)
            elif getattr(job_runner, "client", None) is not None:
                actual_snapshot = fetch_actual_snapshot(job_runner.client)
        except Exception as exc:  # Job/network failures become a structured failed observation round.
            pipeline_error = str(exc)
            for host in (row["source"] for row in reports):
                observations[host].error = observations[host].error or f"ingest failed: {exc}"
        finally:
            if owned_client is not None:
                owned_client.close()

    host_results = [observations[host] for host in targets]
    ok = bool(host_results) and all(
        row.collected and row.ingest_outcome in {"created", "updated", "unchanged"} and not row.error
        for row in host_results
    )
    operation_log.emit("observation_completed", "nodeutils observation completed", ok=ok)
    return ObservationResult(
        ok=ok,
        hosts=host_results,
        collection=collection,
        retrieval=retrieval,
        job=job_result,
        actual=actual_snapshot,
        error=pipeline_error,
    )


def _decode_slurp(path: Path, host: str, *, max_bytes: int, oldest: datetime) -> tuple[str, NodeDump]:
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{host}: invalid slurp JSON: {exc}") from exc
    if not isinstance(envelope, dict) or envelope.get("encoding") != "base64":
        raise ValueError(f"{host}: slurp result is not a base64 envelope")
    try:
        content = base64.b64decode(envelope.get("content", ""), validate=True)
    except (binascii.Error, TypeError) as exc:
        raise ValueError(f"{host}: invalid slurp base64: {exc}") from exc
    if len(content) > max_bytes:
        raise ValueError(f"{host}: report is too large ({len(content)} > {max_bytes} bytes)")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{host}: report is not UTF-8: {exc}") from exc
    dump = parse_dump_text(text, source=host, suffix=".json")
    collected_at = dump.collected_at
    if collected_at.tzinfo is None:
        collected_at = collected_at.replace(tzinfo=timezone.utc)
    if collected_at.astimezone(timezone.utc) < oldest:
        raise ValueError(f"{host}: report is stale: collected_at={collected_at.isoformat()}")
    identities = {
        canonical_node_name(dump.identity.hostname),
        canonical_node_name(getattr(dump.identity, "fqdn", None)),
    }
    identities.discard("")
    if canonical_node_name(host) not in identities:
        raise ValueError(f"{host}: report identity does not match target: {sorted(identities)}")
    return text, dump


def _load_ingest_summary(path: Path, expected_sources: set[str]) -> IngestSummary:
    try:
        summary = IngestSummary.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise ValueError(f"invalid ingest summary artifact: {exc}") from exc
    if summary.schema_version != INGEST_SUMMARY_SCHEMA:
        raise ValueError(f"unsupported ingest summary schema: {summary.schema_version!r}")
    if summary.dry_run:
        raise ValueError("ingest summary unexpectedly reports dry_run=true")
    sources = [row.source for row in summary.results]
    if len(sources) != len(set(sources)) or set(sources) != expected_sources:
        raise ValueError(
            f"ingest summary sources do not match submitted reports: expected={sorted(expected_sources)} actual={sorted(sources)}"
        )
    unsupported = sorted({row.outcome for row in summary.results} - {"created", "updated", "unchanged", "skipped"})
    if unsupported:
        raise ValueError(f"unsupported ingest outcomes: {unsupported}")
    return summary
