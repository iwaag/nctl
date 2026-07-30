"""Tier A contracts for the one-way, pinned LXC creation action."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from nctl_core.drift.compute_creation import ComputeCreation
from nctl_core.drift.model import Target
from nctl_core.reconcile.actions import compute_create
from nctl_core.reconcile.model import ReconcileAction


PARAMETERS = {
    "host_slugs": ["aghub"], "vmid": 109,
    "template": "local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst",
    "storage": "local-lvm", "bridge": "vmbr0", "unprivileged": True,
    "vcpus": 1, "memory_mb": 512, "root_disk_gb": 8,
    "hostname": "agfixture", "mac_address": "bc:24:11:00:01:09",
    "ipv4_cidr": "192.168.0.9/24", "gateway_ipv4": "192.168.0.1",
}


class _Log:
    def __init__(self):
        self.events = []

    def emit(self, *args, **kwargs):
        self.events.append((args, kwargs))


class _Artifacts:
    def __init__(self, root):
        self.root = root

    def path(self, relative):
        return self.root / relative


def _action(parameters=PARAMETERS):
    return ReconcileAction(
        id="create_compute_instance:agfixture", reconciler_id="create_compute_instance",
        action_kind="compute_create", targets=[Target(kind="compute_instance", slug="agfixture", id="instance")],
        claimed_diff_codes=["compute_instance_missing"], reason="test", mutates=True,
        requires_observation=True, parameters=parameters,
    )


def _context(tmp_path):
    return SimpleNamespace(
        artifacts=_Artifacts(tmp_path), round_index=1, snapshot=object(), generated_at="2026-07-28T00:00:00+00:00",
        cfg=SimpleNamespace(
            ansible=SimpleNamespace(
                resolved_playbook_dir=lambda _: tmp_path / "ansible_agdev",
                resolved_inventory=lambda _: tmp_path / "inventories/generated/production.yml",
            ),
            source_path=tmp_path / "nctl.toml",
            reconcile=SimpleNamespace(ansible_timeout_seconds=30),
        ),
        command_runner=lambda *_: None, operation_log=_Log(),
    )


def _creation(parameters=PARAMETERS, failures=()):
    return ComputeCreation(
        instance=SimpleNamespace(), node=SimpleNamespace(slug="agfixture"), platform=SimpleNamespace(),
        cluster=SimpleNamespace(), control_node=SimpleNamespace(slug="aghub"), parameters=parameters,
        failures=failures,
    )


def _install_creation(monkeypatch, creation):
    monkeypatch.setattr(compute_create, "derive_compute_creations", lambda *_args, **_kwargs: {"instance": creation})


def test_create_handler_runs_exactly_one_pinned_playbook_and_requires_local_success_result(tmp_path, monkeypatch):
    """Tier A: the only runner call has the planned host/parameters, then proves local result transport."""
    context = _context(tmp_path)
    _install_creation(monkeypatch, _creation())
    seen = {}

    class Runner:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, command, **_kwargs):
            seen["command"] = command
            result_path = context.artifacts.path("round-01/compute/create_compute_instance:agfixture.result.json")
            result_path.write_text(json.dumps({"created": True, "started": True}))
            return SimpleNamespace(command=command, exit_code=0)

    monkeypatch.setattr(compute_create, "AnsibleRunner", Runner)
    executed = compute_create.execute(context, _action())

    assert executed.result.success is True
    assert executed.result.mutated is True
    assert _action().requires_observation is True
    assert seen["command"][:6] == [
        "ansible-playbook", "-i", str(tmp_path / "inventories/generated/production.yml"),
        str(tmp_path / "ansible_agdev/playbooks/proxmox/create_lxc.yml"), "--limit", "aghub",
    ]
    assert json.loads(seen["command"][7]) == {**PARAMETERS, "result_path": str(context.artifacts.path("round-01/compute/create_compute_instance:agfixture.result.json"))}
    assert context.artifacts.path("round-01/compute/create_compute_instance:agfixture.result.json").parent.is_dir()
    assert all("stop" not in value and "destroy" not in value for value in seen["command"])


@pytest.mark.parametrize("payload", [None, "not-json", {}, {"created": True}, {"created": False, "started": True}])
def test_create_handler_treats_missing_or_untruthful_result_as_mutated_failure(tmp_path, monkeypatch, payload):
    context = _context(tmp_path)
    _install_creation(monkeypatch, _creation())

    class Runner:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, command, **_kwargs):
            if payload is not None:
                path = context.artifacts.path("round-01/compute/create_compute_instance:agfixture.result.json")
                path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
            return SimpleNamespace(command=command, exit_code=0)

    monkeypatch.setattr(compute_create, "AnsibleRunner", Runner)
    result = compute_create.execute(context, _action()).result
    assert result.success is False
    assert result.mutated is True


def test_create_handler_refuses_parameter_drift_before_runner(tmp_path, monkeypatch):
    context = _context(tmp_path)
    _install_creation(monkeypatch, _creation({**PARAMETERS, "vmid": 110}))

    class Runner:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("runner must not start after preflight drift")

    monkeypatch.setattr(compute_create, "AnsibleRunner", Runner)
    result = compute_create.execute(context, _action()).result
    assert result.success is False
    assert result.mutated is False
    assert "pinned preflight" in result.error


def test_create_playbook_has_only_create_start_and_local_result_transport():
    playbook = (compute_create.__file__.replace("nctl/src/nctl_core/reconcile/actions/compute_create.py", "ansible_agdev/playbooks/proxmox/create_lxc.yml"))
    from pathlib import Path
    content = Path(playbook).read_text()
    assert "delegate_to: localhost" in content
    assert "delegate_to: localhost\n      # This is controller-owned evidence" in content
    assert "become: false" in content
    assert "pct_binary: /usr/sbin/pct" in content
    assert "argv: [\"{{ pct_binary }}\", start" in content
    assert "- \"{{ pct_binary }}\"\n          - create" in content
    for forbidden in (" pct stop", " pct destroy", " pct set", " pct resize", " pct migrate", " pct clone", " qm "):
        assert forbidden not in content
