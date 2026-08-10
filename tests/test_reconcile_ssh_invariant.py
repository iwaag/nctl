"""no_guest_vm Step 3: the SSH-contact invariant, executed per reconciler.

For each registered reconciler with an executor handler, build a
representative action, execute the real handler against fakes (a recording
`CommandRunner`, stubbed ledger/Job seams), collect every host the handler
actually contacted (`ansible-playbook`/`ansible` `--limit` values, the
dnsmasq apply's `host_limit`), and assert:

- the contacted set equals `action_host_slugs(action)` for SSH-connecting
  reconcilers;
- reconcilers with `connects_over_ssh=False` contact nothing;
- `connects_over_ssh` is true iff something was contacted.

This is what finally makes `SSH_REQUIRING_RECONCILER_IDS` (derived from the
registry) and each action's declared `host_slugs` falsifiable against the
handlers' real behavior -- incident F3's root cause 1 was exactly that these
facts lived in three hand-maintained structures nothing ever checked.

Seams that stay stubbed, and why that is still honest evidence:

- ledger/Job handlers (`link_actual_node`, `link_compute_realization`,
  `reconcile_ipam`): their downstream `ledger.execute_*` calls are Nautobot
  REST/Job transport with no path to a `CommandRunner` or SSH probe at all;
  the stub records that the handler completed without touching either.
- `dnsmasq_config`: `build_dnsmasq_apply` is recorded at its boundary; that
  `host_limit` is the exact set scanned and deployed is pinned separately by
  `test_dnsmasq_apply.py` (fix_sshkey4 Step 3).
"""

from __future__ import annotations

import json
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from nctl_core.artifacts import OperationArtifacts
from nctl_core.config import Config
from nctl_core.drift.compute_creation import ComputeCreation
from nctl_core.drift.compute_disposition import ComputeDisposition
from nctl_core.drift.model import Target
from nctl_core.reconcile.actions import (
    compute_create,
    compute_destroy,
    compute_link,
    dnsmasq,
    ipam,
    ledger_link,
    observe,
    playbook,
)
from nctl_core.reconcile.actions.contract import ActionContext
from nctl_core.reconcile.actions.dispatch import _HANDLERS, handler_for
from nctl_core.reconcile.model import ReconcileAction
from nctl_core.reconcile.registry import get_reconciler, registered_reconciler_ids
from nctl_core.reconcile.reconcilers import plan_observe_node
from nctl_core.reconcile.ssh_preflight import action_host_slugs
from nctl_core.sources.actual import ActualSnapshot
from nctl_core.sources.desired import DesiredEndpoint, DesiredNode, DesiredSnapshot
from nctl_core.ssh_trust import derive_host_key_alias
from nctl_core.sources.snapshot import SourceSnapshot

# Registered for identity only -- dispatch has no handler, so it cannot be
# exercised; its connects_over_ssh=True is declarative (a playbook run).
PLAN_ONLY_RECONCILER_IDS = {"new_node_baseline"}


def _node_id(host: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"nctl-invariant-node:{host}"))


class ContactRecorder:
    """CommandRunner that records commands and extracts contacted hosts."""

    def __init__(self, result_payload: dict | None = None) -> None:
        self.commands: list[list[str]] = []
        self.result_payload = result_payload
        self.dnsmasq_host_limits: list[list[str]] = []

    def __call__(self, args: list[str], cwd: Path, timeout: float | None) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(args))
        if self.result_payload is not None and "--extra-vars" in args:
            variables = json.loads(args[args.index("--extra-vars") + 1])
            result_path = variables.get("result_path")
            if result_path:
                Path(result_path).parent.mkdir(parents=True, exist_ok=True)
                Path(result_path).write_text(json.dumps(self.result_payload))
        return subprocess.CompletedProcess(args, 0, "", "")

    def contacted_hosts(self) -> set[str]:
        hosts: set[str] = set()
        for command in self.commands:
            if "--limit" in command:
                hosts.update(command[command.index("--limit") + 1].split(","))
        for limit in self.dnsmasq_host_limits:
            hosts.update(limit)
        return hosts


class _Log:
    def emit(self, *args, **kwargs):
        pass


def _config(tmp_path: Path, enrolled: dict[str, str] | None = None) -> Config:
    playbook_dir = tmp_path / "ansible_agdev"
    (playbook_dir / "inventories" / "generated").mkdir(parents=True, exist_ok=True)
    (playbook_dir / "inventories" / "generated" / "production.yml").write_text("all: {}\n")
    known_hosts_file = tmp_path / "ssh" / "known_hosts"
    cfg = Config.model_validate(
        {
            "nautobot": {"url": "http://nautobot.invalid"},
            "inventory": {"dumps_dir": tmp_path / "dumps"},
            "events": {"log_dir": tmp_path / "events"},
            "ansible": {"playbook_dir": playbook_dir, "inventory": "inventories/generated/production.yml"},
            "reconcile": {"nodeutils_version": "a" * 40},
            "ssh": {"known_hosts_file": known_hosts_file},
            "source_path": tmp_path / "nctl.toml",
        }
    )
    known_hosts_file.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"{derive_host_key_alias(node_id)} ssh-ed25519 dGVzdC1vYnNlcnZhdGlvbi1maXh0dXJlLWtleQ== nctl:test\n"
        for node_id in (enrolled or {}).values()
    ]
    known_hosts_file.write_text("".join(lines))
    return cfg


def _context(tmp_path: Path, recorder: ContactRecorder, *, snapshot=None, cfg: Config | None = None) -> ActionContext:
    artifacts = OperationArtifacts.create(tmp_path / "artifacts", "01INVARIANT0000000000000000")
    return ActionContext(
        cfg=cfg or _config(tmp_path),
        operation_log=_Log(),
        artifacts=artifacts,
        round_index=1,
        snapshot=snapshot if snapshot is not None else SourceSnapshot(
            desired=DesiredSnapshot(), actual=ActualSnapshot(), fetched_at=datetime.now(timezone.utc)
        ),
        client=SimpleNamespace(),
        now=lambda: datetime.now(timezone.utc),
        command_runner=recorder,
        ssh_probe=None,
        generated_at="2026-08-10T00:00:00+00:00",
    )


def _assert_invariant(reconciler_id: str, action: ReconcileAction, contacted: set[str]) -> None:
    reconciler = get_reconciler(reconciler_id)
    if reconciler.connects_over_ssh:
        assert contacted == action_host_slugs(action), (
            f"{reconciler_id}: handler contacted {sorted(contacted)} but the action "
            f"declares {sorted(action_host_slugs(action))}"
        )
    else:
        assert contacted == set(), f"{reconciler_id}: ledger-only handler contacted {sorted(contacted)}"
    assert reconciler.connects_over_ssh == bool(contacted), (
        f"{reconciler_id}: connects_over_ssh={reconciler.connects_over_ssh} but "
        f"contacted={sorted(contacted)}"
    )


def test_every_registered_reconciler_is_executable_or_explicitly_plan_only():
    handled = set(_HANDLERS)
    registered = set(registered_reconciler_ids())
    assert registered - handled == PLAN_ONLY_RECONCILER_IDS
    assert handled <= registered


def test_observe_node_contacts_exactly_its_declared_hosts(tmp_path):
    host = "node-a"
    node_id = _node_id(host)
    cfg = _config(tmp_path, enrolled={host: node_id})
    node = DesiredNode(id=node_id, slug=host, name=host, lifecycle="active", node_type="device")
    endpoint = DesiredEndpoint(
        id="endpoint-a", name="primary", endpoint_type="primary",
        node_id=node_id, node_slug=host, mdns_name=f"{host}.local",
    )
    snapshot = SourceSnapshot(
        desired=DesiredSnapshot(nodes=[node], endpoints=[endpoint]),
        actual=ActualSnapshot(),
        fetched_at=datetime.now(timezone.utc),
    )
    recorder = ContactRecorder()
    context = _context(tmp_path, recorder, snapshot=snapshot, cfg=cfg)
    action = plan_observe_node([Target(kind="node", slug=host, name=host, id=node_id)], [])

    observe.execute(context, action)

    _assert_invariant("observe_node", action, recorder.contacted_hosts())


CREATE_PARAMETERS = {
    "guest_type": "lxc", "host_slugs": ["aghub"], "vmid": 110, "template": "local:vztmpl/ubuntu.tar.zst",
    "storage": "local-lvm", "bridge": "vmbr0", "vcpus": 1, "memory_mb": 512, "root_disk_gb": 8,
    "hostname": "agfixture", "mac_address": "bc:24:11:00:00:01", "unprivileged": True,
    "ipv4_cidr": "192.0.2.10/24", "gateway_ipv4": "192.0.2.1",
}


def test_create_compute_instance_contacts_only_the_control_node(tmp_path, monkeypatch):
    recorder = ContactRecorder(result_payload={"created": True, "started": True})
    context = _context(tmp_path, recorder)
    action = ReconcileAction(
        id="create_compute_instance:agfixture", reconciler_id="create_compute_instance",
        action_kind="compute_create", targets=[Target(kind="compute_instance", slug="agfixture", id="instance")],
        claimed_diff_codes=["compute_instance_missing"], reason="test", mutates=True,
        requires_observation=True, parameters=CREATE_PARAMETERS,
    )
    creation = ComputeCreation(
        instance=SimpleNamespace(), node=SimpleNamespace(slug="agfixture"), platform=SimpleNamespace(),
        cluster=SimpleNamespace(), control_node=SimpleNamespace(slug="aghub"),
        parameters=CREATE_PARAMETERS, failures=(),
    )
    monkeypatch.setattr(compute_create, "derive_compute_creations", lambda *_a, **_k: {"instance": creation})

    result = compute_create.execute(context, action).result

    assert result.success
    _assert_invariant("create_compute_instance", action, recorder.contacted_hosts())


DESTROY_PARAMETERS = {
    "compute_instance_id": "instance", "desired_node_id": "node", "desired_node_slug": "agfixture",
    "compute_platform_id": "platform", "compute_platform_slug": "aghub-pve", "cluster_id": "cluster",
    "virtual_machine_id": "vm", "guest_type": "lxc", "vmid": 110, "observed_proxmox_node": "aghub",
    "control_desired_node_id": "control", "control_desired_node_slug": "aghub", "host_slugs": ["aghub"],
}


def test_destroy_compute_instance_contacts_only_the_control_node(tmp_path, monkeypatch):
    recorder = ContactRecorder(result_payload={"destroyed": True, "absent": True})
    context = _context(tmp_path, recorder)
    action = ReconcileAction(
        id="destroy_compute_instance:agfixture", reconciler_id="destroy_compute_instance",
        action_kind="compute_destroy", targets=[Target(kind="compute_instance", slug="agfixture", id="instance")],
        claimed_diff_codes=["compute_instance_destroy_required"], reason="test", mutates=True,
        requires_observation=True, parameters=DESTROY_PARAMETERS,
    )
    monkeypatch.setattr(
        compute_destroy, "derive_compute_dispositions",
        lambda *_a, **_k: {"instance": ComputeDisposition("destroy_required", None, DESTROY_PARAMETERS)},
    )

    result = compute_destroy.execute(context, action).result

    assert result.success
    # The gate that bit in incident F3, stated positively: the guest's own
    # slug is never in the contacted set, so its enrollment can never gate
    # this destroy.
    assert "agfixture" not in recorder.contacted_hosts()
    _assert_invariant("destroy_compute_instance", action, recorder.contacted_hosts())


def test_service_profile_contacts_exactly_its_declared_hosts(tmp_path):
    recorder = ContactRecorder()
    context = _context(tmp_path, recorder)
    action = ReconcileAction(
        id="service_profile:web:websvc", reconciler_id="service_profile", action_kind="playbook",
        targets=[Target(kind="service", slug="websvc")], claimed_diff_codes=["service_not_running"],
        reason="test", mutates=True, requires_observation=True,
        parameters={"deployment_profile": "web", "host_slugs": ["agdb", "agweb"], "playbook": "playbooks/web.yml"},
    )

    result = playbook.execute(context, action).result

    assert result.success
    _assert_invariant("service_profile", action, recorder.contacted_hosts())


def test_dnsmasq_config_passes_exactly_its_declared_hosts_as_host_limit(tmp_path, monkeypatch):
    recorder = ContactRecorder()
    context = _context(tmp_path, recorder)
    action = ReconcileAction(
        id="dnsmasq_config:dnsmasq:dnsmasq", reconciler_id="dnsmasq_config", action_kind="dnsmasq_config",
        targets=[Target(kind="service", slug="dnsmasq")], claimed_diff_codes=["service_config_mismatch"],
        reason="test", mutates=True, requires_observation=True,
        parameters={"deployment_profile": "dnsmasq", "host_slugs": ["agdnsmasq"]},
    )

    def fake_build_dnsmasq_apply(cfg, apply_changes, probe, host_limit):
        recorder.dnsmasq_host_limits.append(list(host_limit))
        return SimpleNamespace(ok=True)

    monkeypatch.setattr(dnsmasq, "build_dnsmasq_apply", fake_build_dnsmasq_apply)

    result = dnsmasq.execute(context, action).result

    assert result.success
    _assert_invariant("dnsmasq_config", action, recorder.contacted_hosts())


class _LedgerResult:
    def model_dump(self):
        return {}


def test_link_actual_node_contacts_nothing(tmp_path, monkeypatch):
    recorder = ContactRecorder()
    context = _context(tmp_path, recorder)
    action = ReconcileAction(
        id="link_actual_node:agweb", reconciler_id="link_actual_node", action_kind="ledger_patch",
        targets=[Target(kind="node", slug="agweb", id="n1")], claimed_diff_codes=["actual_node_not_linked"],
        reason="test", mutates=True, requires_observation=False,
        parameters={"candidate": {"id": "dev-1", "object_type": "dcim.device"}},
    )
    monkeypatch.setattr(ledger_link, "execute_link_actual_node", lambda *_a, **_k: _LedgerResult())

    result = ledger_link.execute(context, action).result

    assert result.success
    _assert_invariant("link_actual_node", action, recorder.contacted_hosts())


def test_link_compute_realization_contacts_nothing(tmp_path, monkeypatch):
    recorder = ContactRecorder()
    context = _context(tmp_path, recorder)
    parameters = {
        "compute_platform_id": "platform", "compute_instance_id": "instance", "cluster_id": "cluster",
        "virtual_machine_id": "vm", "match_basis": "vmid", "vmid": 110,
    }
    action = ReconcileAction(
        id="link_compute_realization:agfixture", reconciler_id="link_compute_realization",
        action_kind="ledger_patch", targets=[Target(kind="compute_instance", slug="agfixture", id="instance")],
        claimed_diff_codes=["compute_instance_not_linked"], reason="test", mutates=True,
        requires_observation=False, parameters=parameters,
    )
    realization = SimpleNamespace(
        platform=SimpleNamespace(id="platform"), instance=SimpleNamespace(id="instance"),
        cluster=SimpleNamespace(id="cluster"), virtual_machine=SimpleNamespace(id="vm"), match_basis="vmid",
    )
    monkeypatch.setattr(compute_link, "derive_compute_realizations", lambda *_a, **_k: {"instance": realization})
    monkeypatch.setattr(compute_link, "execute_link_compute_realization", lambda *_a, **_k: _LedgerResult())

    result = compute_link.execute(context, action).result

    assert result.success
    _assert_invariant("link_compute_realization", action, recorder.contacted_hosts())


def test_reconcile_ipam_contacts_nothing(tmp_path, monkeypatch):
    recorder = ContactRecorder()
    context = _context(tmp_path, recorder)
    action = ReconcileAction(
        id="reconcile_ipam:agweb", reconciler_id="reconcile_ipam", action_kind="job",
        targets=[Target(kind="node", slug="agweb", id="n1")], claimed_diff_codes=["missing_actual_ip_address"],
        reason="test", mutates=True, requires_observation=False,
        parameters={"desired_node_slug": "agweb", "eligible_endpoint_ids": []},
    )
    ipam_result = SimpleNamespace(
        unresolved_expected_endpoints=[], conflicts=[], skipped=[], eligible_endpoint_ids=[], applied_endpoint_ids=[]
    )
    monkeypatch.setattr(ipam, "execute_reconcile_ipam", lambda *_a, **_k: ipam_result)

    result = ipam.execute(context, action).result

    assert result.success
    _assert_invariant("reconcile_ipam", action, recorder.contacted_hosts())


def test_plan_only_reconcilers_have_no_handler():
    for reconciler_id in PLAN_ONLY_RECONCILER_IDS:
        assert handler_for(reconciler_id) is None
