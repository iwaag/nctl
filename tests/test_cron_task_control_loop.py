"""autotask_intent Step 3: one real control-loop test (README_DEV lesson 8).

Follows the real probe-hint renderer, drift evaluator, and reconcile planner
through the transition a `cron_task` placement claims to support:

    placement with config.script_path
      -> hints rendered (real repo deployment_profiles.yml metadata)
      -> simulated observation without the file -> service_missing
      -> simulated observation with file + crontab entry -> satisfied,
         no planned action

autotask_intent ex1 extends the profile with a `cron_registered` check:
existence of the script alone no longer converges -- registration proof
must also pass. The observed entries mirror the exact shape nodeutils'
`normalize_observed_services` emits for hinted state-proof checks (covered
by nodeutils' own suite), so the two suites pin the same contract from
both sides.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml

from nctl_core.drift.model import Severity, Target, DiffRecord
from nctl_core.drift.service_placement import evaluate_placement_drift
from nctl_core.observation import render_probe_hints
from nctl_core.production.profiles import load_deployment_profiles
from nctl_core.reconcile.model import PlanScope
from nctl_core.reconcile.planner import build_plan
from nctl_core.reconcile.profiles import load_profile_reconciliation
from nctl_core.sources.actual import ActualSnapshot
from nctl_core.sources.desired import (
    DesiredNode,
    DesiredService,
    DesiredServicePlacement,
    DesiredSnapshot,
)
from nctl_core.sources.snapshot import SourceSnapshot

NOW = datetime(2026, 8, 7, 0, 0, tzinfo=timezone.utc)
SCRIPT_PATH = "/home/eiji/mycron/heartbeat.sh"
NODE_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, "nctl-test-node:agpc"))


def _repo_playbook_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "ansible_agdev"


def _desired() -> DesiredSnapshot:
    node = DesiredNode(
        id=NODE_ID, slug="agpc", name="agpc", lifecycle="active", node_type="device",
        realized_device_id="d1",
    )
    service = DesiredService(id="svc-hb", slug="heartbeat-cron", name="heartbeat-cron", lifecycle="active")
    placement = DesiredServicePlacement(
        id="p1", service_id="svc-hb", node_id=NODE_ID, instance_name="heartbeat",
        deployment_profile="cron_task", config_schema_version="1",
        config={"script_path": SCRIPT_PATH},
    )
    return DesiredSnapshot(nodes=[node], services=[service], placements=[placement])


def _real_reconciliation():
    playbook_dir = _repo_playbook_dir()
    profiles, _digest = load_deployment_profiles(playbook_dir)
    return load_profile_reconciliation(playbook_dir, set(profiles))


def _evaluate(observed_services: dict) -> dict:
    devices = {
        "d1": {
            "observed_system": "Linux",
            "observed_services": observed_services,
            "service_inventory_updated_at": "2026-08-06T23:00:00+00:00",
        }
    }
    return evaluate_placement_drift(
        [{"id": "svc-hb", "name": "heartbeat-cron"}],
        [
            {
                "placement_id": "p1",
                "service_id": "svc-hb",
                "node_id": NODE_ID,
                "node_slug": "agpc",
                "instance_name": "heartbeat",
                "deployment_profile": "cron_task",
                "management_mode": "nctl_managed",
                "realized_device_id": "d1",
                "host_os": "linux",
            }
        ],
        devices,
        {"d1": NODE_ID},
        now=NOW,
        stale_after_hours=24,
    )["svc-hb"]


def test_cron_task_round_trip_missing_then_converged_with_no_planned_action():
    reconciliation = _real_reconciliation()
    desired = _desired()

    # Round 0: the real repo metadata renders the config-resolved hints --
    # ex1: existence proof and registration proof, both from script_path.
    hints = yaml.safe_load(render_probe_hints(desired, NODE_ID, reconciliation))
    assert hints["service_probe_hints"]["heartbeat-cron"] == {
        "checks": [
            {"kind": "file_exists", "path": SCRIPT_PATH},
            {"kind": "cron_registered", "path": SCRIPT_PATH},
        ]
    }

    # Round 1: observation ran, file absent and crontab empty -- the entry is
    # positive evidence the checks executed, and drift must say service_missing.
    missing_entry = {
        "heartbeat-cron": {
            "state": "missing",
            "source": "check:cron_registered+file_exists",
            "checked_at": "2026-08-06T23:00:00+00:00",
            "checks": [
                {"kind": "file_exists", "path": SCRIPT_PATH, "status": "missing"},
                {"kind": "cron_registered", "path": SCRIPT_PATH, "status": "missing"},
            ],
        }
    }
    report = _evaluate(missing_entry)
    assert report["status"] == "drift"
    [placement_report] = report["placements"]
    assert [gap["code"] for gap in placement_report["gaps"]] == ["service_missing"]
    assert placement_report["observed_checks"][0]["status"] == "missing"

    # The planner must not invent an action for an observe_only profile: the
    # drift stays visible as unsupported, with zero planned mutations.
    snapshot = SourceSnapshot(
        desired=desired, actual=ActualSnapshot(), fetched_at=NOW,
    )
    diff = DiffRecord(
        target=Target(kind="service", slug="heartbeat-cron", name="heartbeat-cron", id="svc-hb"),
        code="service_missing", severity=Severity.ERROR, message="heartbeat-cron: service_missing",
    )
    plan = build_plan(
        snapshot=snapshot, diffs=[diff], scope=PlanScope(kind="cluster"),
        drift_generated_at=NOW.isoformat(), profile_reconciliation=reconciliation,
    )
    assert plan.actions == []
    assert plan.unsupported[0].reason.startswith("deployment profile 'cron_task' is observe_only")

    # Round 2 (ex1): file placed but crontab not yet registered -- existence
    # proof alone must NOT converge; the failed registration proof keeps the
    # placement service_missing.
    unregistered_entry = {
        "heartbeat-cron": {
            "state": "missing",
            "source": "check:cron_registered+file_exists",
            "checked_at": "2026-08-06T23:15:00+00:00",
            "checks": [
                {"kind": "file_exists", "path": SCRIPT_PATH, "status": "present"},
                {"kind": "cron_registered", "path": SCRIPT_PATH, "status": "missing"},
            ],
        }
    }
    report = _evaluate(unregistered_entry)
    assert report["status"] == "drift"
    [placement_report] = report["placements"]
    assert [gap["code"] for gap in placement_report["gaps"]] == ["service_missing"]

    # Round 3: file placed and crontab registered, fresh observation -- both
    # state proofs satisfied, no gap, and (observe-only) still nothing to plan.
    present_entry = {
        "heartbeat-cron": {
            "state": "present",
            "source": "check:cron_registered+file_exists",
            "checked_at": "2026-08-06T23:30:00+00:00",
            "checks": [
                {"kind": "file_exists", "path": SCRIPT_PATH, "status": "present"},
                {"kind": "cron_registered", "path": SCRIPT_PATH, "status": "present"},
            ],
        }
    }
    report = _evaluate(present_entry)
    assert report["status"] == "satisfied"
    [placement_report] = report["placements"]
    assert placement_report["gaps"] == []


def test_cron_registered_error_status_fails_closed():
    # An unusable crontab tool reports status "error" -- never proof either
    # way, so even beside richer running-state evidence the errored
    # registration proof must stay service_missing, not read as converged.
    entry = {
        "heartbeat-cron": {
            "state": "active",
            "source": "process",
            "checked_at": "2026-08-06T23:00:00+00:00",
            "checks": [
                {"kind": "file_exists", "path": SCRIPT_PATH, "status": "present"},
                {"kind": "cron_registered", "path": SCRIPT_PATH, "status": "error"},
            ],
        }
    }
    report = _evaluate(entry)
    assert [gap["code"] for gap in report["placements"][0]["gaps"]] == ["service_missing"]


def test_running_entry_with_failed_existence_check_is_service_missing():
    # A richer running-state detection must not mask a failed existence
    # proof: the file_exists miss in `checks` alone forces service_missing.
    entry = {
        "heartbeat-cron": {
            "state": "active",
            "source": "process",
            "checked_at": "2026-08-06T23:00:00+00:00",
            "checks": [{"kind": "file_exists", "path": SCRIPT_PATH, "status": "missing"}],
        }
    }
    report = _evaluate(entry)
    assert [gap["code"] for gap in report["placements"][0]["gaps"]] == ["service_missing"]
