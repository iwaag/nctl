"""Tier A contracts for the one-way, pinned LXC destruction action."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from nctl_core.drift.compute_disposition import ComputeDisposition
from nctl_core.drift.model import Target
from nctl_core.reconcile.actions import compute_destroy
from nctl_core.reconcile.model import ReconcileAction


PARAMETERS = {
    "compute_instance_id": "instance", "desired_node_id": "node", "desired_node_slug": "agfixture",
    "compute_platform_id": "platform", "compute_platform_slug": "aghub-pve", "cluster_id": "cluster",
    "virtual_machine_id": "vm", "guest_type": "lxc", "vmid": 109, "observed_proxmox_node": "aghub",
    "control_desired_node_id": "control", "control_desired_node_slug": "aghub", "host_slugs": ["aghub"],
}


class _Log:
    def emit(self, *args, **kwargs):
        pass


class _Artifacts:
    def __init__(self, root): self.root = root
    def path(self, relative): return self.root / relative


def _action(parameters=PARAMETERS):
    return ReconcileAction(
        id="destroy_compute_instance:agfixture", reconciler_id="destroy_compute_instance",
        action_kind="compute_destroy", targets=[Target(kind="compute_instance", slug="agfixture", id="instance")],
        claimed_diff_codes=["compute_instance_destroy_required"], reason="test", mutates=True,
        requires_observation=True, parameters=parameters,
    )


def _context(tmp_path):
    return SimpleNamespace(
        artifacts=_Artifacts(tmp_path), round_index=1, snapshot=object(), generated_at="2026-07-30T00:00:00+00:00",
        cfg=SimpleNamespace(
            ansible=SimpleNamespace(
                resolved_playbook_dir=lambda _: tmp_path / "ansible_agdev",
                resolved_inventory=lambda _: tmp_path / "inventories/generated/production.yml",
            ), source_path=tmp_path / "nctl.toml", reconcile=SimpleNamespace(ansible_timeout_seconds=30),
        ), command_runner=lambda *_: None, operation_log=_Log(),
    )


def _install_disposition(monkeypatch, parameters=PARAMETERS, outcome="destroy_required"):
    monkeypatch.setattr(compute_destroy, "derive_compute_dispositions", lambda *_args, **_kwargs: {
        "instance": ComputeDisposition(outcome, None, parameters)
    })


def test_destroy_handler_runs_one_pinned_playbook_and_confirms_absence(tmp_path, monkeypatch):
    context = _context(tmp_path)
    _install_disposition(monkeypatch)
    seen = {}
    class Runner:
        def __init__(self, *_args, **_kwargs): pass
        def run(self, command, **_kwargs):
            seen["command"] = command
            context.artifacts.path("round-01/compute/destroy_compute_instance:agfixture.result.json").write_text(
                json.dumps({"destroyed": True, "absent": True})
            )
            return SimpleNamespace(command=command, exit_code=0)
    monkeypatch.setattr(compute_destroy, "AnsibleRunner", Runner)
    result = compute_destroy.execute(context, _action()).result
    assert result.success and result.mutated
    assert seen["command"][:6] == ["ansible-playbook", "-i", str(tmp_path / "inventories/generated/production.yml"), str(tmp_path / "ansible_agdev/playbooks/proxmox/destroy_lxc.yml"), "--limit", "aghub"]
    assert json.loads(seen["command"][7]) == {**PARAMETERS, "result_path": str(context.artifacts.path("round-01/compute/destroy_compute_instance:agfixture.result.json"))}


def test_destroy_handler_runs_qemu_playbook_for_virtual_machine_guest(tmp_path, monkeypatch):
    context = _context(tmp_path)
    qemu_parameters = {**PARAMETERS, "guest_type": "qemu"}
    _install_disposition(monkeypatch, qemu_parameters)
    seen = {}
    class Runner:
        def __init__(self, *_args, **_kwargs): pass
        def run(self, command, **_kwargs):
            seen["command"] = command
            context.artifacts.path("round-01/compute/destroy_compute_instance:agfixture.result.json").write_text(
                json.dumps({"destroyed": True, "absent": True})
            )
            return SimpleNamespace(command=command, exit_code=0)
    monkeypatch.setattr(compute_destroy, "AnsibleRunner", Runner)
    result = compute_destroy.execute(context, _action(qemu_parameters)).result
    assert result.success and result.mutated
    assert seen["command"][3] == str(tmp_path / "ansible_agdev/playbooks/proxmox/destroy_qemu.yml")


def test_destroy_handler_refuses_changed_disposition_before_runner(tmp_path, monkeypatch):
    _install_disposition(monkeypatch, {**PARAMETERS, "vmid": 110})
    class Runner:
        def __init__(self, *_args, **_kwargs): raise AssertionError("runner must not start")
    monkeypatch.setattr(compute_destroy, "AnsibleRunner", Runner)
    result = compute_destroy.execute(_context(tmp_path), _action()).result
    assert not result.success and not result.mutated and "pinned disposition" in result.error


@pytest.mark.parametrize("payload", [None, "not-json", {}, {"absent": False}])
def test_destroy_handler_treats_uncertain_result_as_mutated_failure(tmp_path, monkeypatch, payload):
    context = _context(tmp_path)
    _install_disposition(monkeypatch)
    class Runner:
        def __init__(self, *_args, **_kwargs): pass
        def run(self, command, **_kwargs):
            if payload is not None:
                path = context.artifacts.path("round-01/compute/destroy_compute_instance:agfixture.result.json")
                path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
            return SimpleNamespace(command=command, exit_code=0)
    monkeypatch.setattr(compute_destroy, "AnsibleRunner", Runner)
    result = compute_destroy.execute(context, _action()).result
    assert not result.success and result.mutated


def test_destroy_handler_reports_already_absent_without_mutation(tmp_path, monkeypatch):
    context = _context(tmp_path)
    _install_disposition(monkeypatch)
    class Runner:
        def __init__(self, *_args, **_kwargs): pass
        def run(self, command, **_kwargs):
            context.artifacts.path("round-01/compute/destroy_compute_instance:agfixture.result.json").write_text(
                json.dumps({"destroyed": False, "absent": True})
            )
            return SimpleNamespace(command=command, exit_code=0)
    monkeypatch.setattr(compute_destroy, "AnsibleRunner", Runner)
    result = compute_destroy.execute(context, _action()).result
    assert result.success and not result.mutated
