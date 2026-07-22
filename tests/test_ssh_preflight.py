from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

from nctl_core.config import Config
from nctl_core.drift.model import Target
from nctl_core.production.composer import ResolvedSshTarget
from nctl_core.reconcile.model import PlanScope, ReconcileAction, ReconcilePlan
from nctl_core.reconcile.ssh_preflight import (
    STATUS_MISMATCH,
    STATUS_READY,
    STATUS_UNENROLLED,
    STATUS_UNREACHABLE,
    check_ssh_enrollment,
    ssh_required_host_slugs,
    verify_offered_keys,
    verify_resolved_ssh_targets,
)
from nctl_core.ssh_enroll import SshProbeRunner
from nctl_core.ssh_trust import compute_sha256_fingerprint, derive_host_key_alias
from nctl_core.sources.desired import (
    DesiredEndpoint,
    DesiredNode,
    DesiredSnapshot,
)

NODE_ID = "27818c12-fe15-4c9f-83d0-7949523f6c33"
LEDGER_NODE_ID = "00000000-0000-0000-0000-000000000002"
KEY_BLOB = "QUFBQUMzTnphQzFsWkRJMU5URTVBQUFBSUZmYWtlZWQyNTUxOWtleWJ5dGVzMDAwMDAwMDAwMDAwMDAwMA=="
OTHER_KEY_BLOB = "QUFBQUMzTnphQzFsWkRJMU5URTVBQUFBSUZmYWtlZWQyNTUxOWRpZmZlcmVudGtleWJ5dGVzMDAwMDA="


def _config(tmp_path: Path) -> Config:
    config_path = tmp_path / "nctl.toml"
    config_path.write_text(
        f"""
[nautobot]
url = "http://nautobot.test"

[inventory]
dumps_dir = "{tmp_path / 'dumps'}"

[events]
log_dir = "{tmp_path / 'events'}"

[ansible]
playbook_dir = "{tmp_path / 'ansible_agdev'}"
inventory = "inventories/generated/hosts_intent.yml"

[repo]
root = "{tmp_path}"

[ssh]
known_hosts_file = "{tmp_path / 'ssh' / 'known_hosts'}"
lock_path = "{tmp_path / 'ssh.lock'}"
"""
    )
    (tmp_path / "ansible_agdev" / "inventories" / "generated").mkdir(parents=True)
    (tmp_path / "ansible_agdev" / "inventories" / "generated" / "hosts_intent.yml").write_text("all: {}\n")
    return Config.load(config_path)


def _snapshot() -> DesiredSnapshot:
    return DesiredSnapshot(
        nodes=[
            DesiredNode(id=NODE_ID, slug="agdnsmasq", name="agdnsmasq", lifecycle="active", node_type="device"),
            DesiredNode(id=LEDGER_NODE_ID, slug="agledgeronly", name="agledgeronly", lifecycle="active", node_type="device"),
        ],
        endpoints=[
            DesiredEndpoint(
                id="endpoint-1",
                name="primary",
                endpoint_type="primary",
                node_id=NODE_ID,
                node_slug="agdnsmasq",
                mdns_name="agdnsmasq.local",
            ),
        ],
    )


def _plan(*actions: ReconcileAction) -> ReconcilePlan:
    return ReconcilePlan(
        scope=PlanScope(kind="cluster"),
        drift_fingerprint="fp",
        generated_at=datetime.now(timezone.utc),
        actions=list(actions),
    )


def _action(reconciler_id: str, action_kind: str, slug: str) -> ReconcileAction:
    return ReconcileAction(
        id=f"action-{slug}",
        reconciler_id=reconciler_id,
        action_kind=action_kind,
        targets=[Target(kind="node", slug=slug)],
        claimed_diff_codes=[],
        reason="test",
        mutates=True,
        requires_observation=False,
    )


def _service_action(reconciler_id: str, action_kind: str, service_slug: str, host_slugs: list[str]) -> ReconcileAction:
    """Mirrors reconcilers.plan_service_profile: targets are the *service*
    (kind="service"), and the actual node slugs live in parameters["host_slugs"]."""
    return ReconcileAction(
        id=f"action-{service_slug}",
        reconciler_id=reconciler_id,
        action_kind=action_kind,
        targets=[Target(kind="service", slug=service_slug)],
        claimed_diff_codes=[],
        reason="test",
        mutates=True,
        requires_observation=False,
        parameters={"host_slugs": host_slugs},
    )


def test_ssh_required_host_slugs_reads_host_slugs_param_for_service_actions():
    plan = _plan(
        _service_action("service_profile", "playbook", "web", ["agweb"]),
        _service_action("dnsmasq_config", "dnsmasq_config", "dnsmasq", ["agdnsmasq"]),
    )
    assert ssh_required_host_slugs(plan) == {"agweb", "agdnsmasq"}


def test_ssh_required_host_slugs_includes_observe_and_playbook_actions():
    plan = _plan(
        _action("observe_node", "observation", "agdnsmasq"),
        _action("service_profile", "playbook", "agsvc"),
        _action("dnsmasq_config", "dnsmasq_config", "agdns2"),
    )
    assert ssh_required_host_slugs(plan) == {"agdnsmasq", "agsvc", "agdns2"}


def test_ssh_required_host_slugs_can_be_narrowed_to_observe_node_only():
    plan = _plan(
        _action("observe_node", "observation", "agdnsmasq"),
        _action("service_profile", "playbook", "agsvc"),
    )
    assert ssh_required_host_slugs(plan, reconciler_ids=frozenset({"observe_node"})) == {"agdnsmasq"}


def test_ssh_required_host_slugs_excludes_ledger_only_actions():
    plan = _plan(
        _action("link_actual_node", "ledger_patch", "agledgeronly"),
        _action("reconcile_ipam", "job", "agledgeronly"),
    )
    assert ssh_required_host_slugs(plan) == set()


def _write_managed_entry(cfg: Config, lookup_name: str, key_blob: str = KEY_BLOB) -> None:
    path = cfg.resolved_ssh_known_hosts_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{lookup_name} ssh-ed25519 {key_blob} nctl:test\n")


def test_check_ssh_enrollment_reports_unenrolled_when_missing(tmp_path):
    cfg = _config(tmp_path)
    entries = check_ssh_enrollment(cfg, ["agdnsmasq"], _snapshot())
    assert len(entries) == 1
    assert entries[0].status == STATUS_UNENROLLED
    assert entries[0].alias == derive_host_key_alias(NODE_ID)


def test_check_ssh_enrollment_reports_ready_when_present(tmp_path):
    cfg = _config(tmp_path)
    alias = derive_host_key_alias(NODE_ID)
    _write_managed_entry(cfg, alias)
    entries = check_ssh_enrollment(cfg, ["agdnsmasq"], _snapshot())
    assert entries[0].status == STATUS_READY


def test_check_ssh_enrollment_reports_unenrolled_for_unknown_host(tmp_path):
    cfg = _config(tmp_path)
    entries = check_ssh_enrollment(cfg, ["does-not-exist"], _snapshot())
    assert entries[0].status == STATUS_UNENROLLED
    assert entries[0].detail == "unknown_host"


def _probe(*, keyscan_stdout: str = "", keyscan_raises: Exception | None = None) -> SshProbeRunner:
    def keyscan(host, port, timeout):
        if keyscan_raises is not None:
            raise keyscan_raises
        return subprocess.CompletedProcess(args=["ssh-keyscan"], returncode=0, stdout=keyscan_stdout, stderr="")

    return SshProbeRunner(keyscan=keyscan, effective_config=lambda host, port: subprocess.CompletedProcess([], 0, "", ""), keygen_find=lambda p, h: subprocess.CompletedProcess([], 0, "", ""))


def test_verify_offered_keys_matching_key_is_ready(tmp_path):
    cfg = _config(tmp_path)
    alias = derive_host_key_alias(NODE_ID)
    _write_managed_entry(cfg, alias)
    probe = _probe(keyscan_stdout=f"agdnsmasq.local ssh-ed25519 {KEY_BLOB}\n")
    entries = verify_offered_keys(cfg, ["agdnsmasq"], _snapshot(), probe)
    assert entries[0].status == STATUS_READY


def test_verify_offered_keys_mismatch_is_reported(tmp_path):
    cfg = _config(tmp_path)
    alias = derive_host_key_alias(NODE_ID)
    _write_managed_entry(cfg, alias)
    probe = _probe(keyscan_stdout=f"agdnsmasq.local ssh-ed25519 {OTHER_KEY_BLOB}\n")
    entries = verify_offered_keys(cfg, ["agdnsmasq"], _snapshot(), probe)
    assert entries[0].status == STATUS_MISMATCH


def test_verify_offered_keys_unreachable_on_timeout(tmp_path):
    cfg = _config(tmp_path)
    alias = derive_host_key_alias(NODE_ID)
    _write_managed_entry(cfg, alias)
    probe = _probe(keyscan_raises=subprocess.TimeoutExpired(cmd=["ssh-keyscan"], timeout=1))
    entries = verify_offered_keys(cfg, ["agdnsmasq"], _snapshot(), probe)
    assert entries[0].status == STATUS_UNREACHABLE


def test_verify_offered_keys_skips_scan_when_unenrolled(tmp_path):
    cfg = _config(tmp_path)
    probe = _probe(keyscan_raises=AssertionError("should not be called"))
    entries = verify_offered_keys(cfg, ["agdnsmasq"], _snapshot(), probe)
    assert entries[0].status == STATUS_UNENROLLED


def _target(*, slug="agdnsmasq", node_id=NODE_ID, route="192.168.0.2", port=22, generation_id="gen-1") -> ResolvedSshTarget:
    return ResolvedSshTarget(
        slug=slug, desired_node_id=node_id, alias=derive_host_key_alias(node_id), route=route, port=port,
        generation_id=generation_id,
    )


def test_verify_resolved_ssh_targets_scans_the_targets_own_route_and_port(tmp_path):
    cfg = _config(tmp_path)
    alias = derive_host_key_alias(NODE_ID)
    _write_managed_entry(cfg, alias)
    scanned = []

    def keyscan(host, port, timeout):
        scanned.append((host, port))
        return subprocess.CompletedProcess(args=["ssh-keyscan"], returncode=0, stdout=f"{host} ssh-ed25519 {KEY_BLOB}\n", stderr="")

    probe = SshProbeRunner(keyscan=keyscan, effective_config=lambda h, p: subprocess.CompletedProcess([], 0, "", ""), keygen_find=lambda p, h: subprocess.CompletedProcess([], 0, "", ""))
    target = _target(route="192.168.0.2", port=2222, generation_id="gen-42")

    entries = verify_resolved_ssh_targets(cfg, ["agdnsmasq"], {"agdnsmasq": target}, probe, round_index=3)

    assert scanned == [("192.168.0.2", 2222)]
    assert entries[0].status == STATUS_READY
    assert entries[0].route == "192.168.0.2"
    assert entries[0].port == 2222
    assert entries[0].generation_id == "gen-42"
    assert entries[0].round == 3
    assert entries[0].phase == "production_route"
    assert entries[0].offered_fingerprints
    assert entries[0].managed_fingerprints


def test_verify_resolved_ssh_targets_old_snapshot_port_vs_new_generation_port(tmp_path):
    # fix_sshkey3 Step 2 required regression test: only the new generation's
    # port is ever scanned -- there is no round-start snapshot port to fall
    # back to or combine with.
    cfg = _config(tmp_path)
    alias = derive_host_key_alias(NODE_ID)
    _write_managed_entry(cfg, alias)
    scanned = []

    def keyscan(host, port, timeout):
        scanned.append(port)
        return subprocess.CompletedProcess(args=["ssh-keyscan"], returncode=0, stdout=f"{host} ssh-ed25519 {KEY_BLOB}\n", stderr="")

    probe = SshProbeRunner(keyscan=keyscan, effective_config=lambda h, p: subprocess.CompletedProcess([], 0, "", ""), keygen_find=lambda p, h: subprocess.CompletedProcess([], 0, "", ""))
    target = _target(port=2222)  # the new generation's port; the "old snapshot" would have said 22

    verify_resolved_ssh_targets(cfg, ["agdnsmasq"], {"agdnsmasq": target}, probe)

    assert scanned == [2222]


def test_verify_resolved_ssh_targets_missing_from_map_is_rejected(tmp_path):
    # A node with a resolvable source route but skipped from production
    # composition (e.g. a local composition error) never gets a
    # ResolvedSshTarget -- it must be rejected before any service Ansible,
    # never silently scanned another way.
    cfg = _config(tmp_path)
    probe = _probe(keyscan_raises=AssertionError("keyscan must not run for a target missing from the map"))

    entries = verify_resolved_ssh_targets(cfg, ["agdnsmasq"], {}, probe)

    assert entries[0].status == STATUS_UNREACHABLE
    assert entries[0].detail == "no_resolvable_production_target"


def test_verify_resolved_ssh_targets_partial_map_does_not_escape_other_target(tmp_path):
    cfg = _config(tmp_path)
    known_hosts_path = cfg.resolved_ssh_known_hosts_file()
    known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
    known_hosts_path.write_text(f"{derive_host_key_alias(NODE_ID)} ssh-ed25519 {KEY_BLOB} nctl:test\n")
    scanned_hosts = []

    def keyscan(host, port, timeout):
        scanned_hosts.append(host)
        return subprocess.CompletedProcess(args=["ssh-keyscan"], returncode=0, stdout=f"{host} ssh-ed25519 {KEY_BLOB}\n", stderr="")

    probe = SshProbeRunner(keyscan=keyscan, effective_config=lambda h, p: subprocess.CompletedProcess([], 0, "", ""), keygen_find=lambda p, h: subprocess.CompletedProcess([], 0, "", ""))
    target = _target(route="192.168.0.2")

    entries = verify_resolved_ssh_targets(cfg, ["agdnsmasq", "agledgeronly"], {"agdnsmasq": target}, probe)

    by_slug = {e.slug: e for e in entries}
    assert scanned_hosts == ["192.168.0.2"]  # never a route for agledgeronly
    assert by_slug["agdnsmasq"].status == STATUS_READY
    assert by_slug["agledgeronly"].status == STATUS_UNREACHABLE
    assert by_slug["agledgeronly"].detail == "no_resolvable_production_target"


def test_verify_resolved_ssh_targets_unenrolled_when_managed_store_lacks_alias(tmp_path):
    cfg = _config(tmp_path)
    probe = _probe(keyscan_raises=AssertionError("should not be called"))
    target = _target()

    entries = verify_resolved_ssh_targets(cfg, ["agdnsmasq"], {"agdnsmasq": target}, probe)

    assert entries[0].status == STATUS_UNENROLLED


def test_verify_resolved_ssh_targets_mismatch(tmp_path):
    cfg = _config(tmp_path)
    _write_managed_entry(cfg, derive_host_key_alias(NODE_ID), KEY_BLOB)
    probe = _probe(keyscan_stdout=f"192.168.0.2 ssh-ed25519 {OTHER_KEY_BLOB}\n")
    target = _target()

    entries = verify_resolved_ssh_targets(cfg, ["agdnsmasq"], {"agdnsmasq": target}, probe)

    assert entries[0].status == STATUS_MISMATCH


def test_verify_offered_keys_bootstrap_mode_still_uses_mdns(tmp_path):
    # verify_offered_keys is bootstrap-only now (fix_sshkey3 Step 2) -- no
    # route_overrides parameter at all, always the mDNS endpoint.
    cfg = _config(tmp_path)
    alias = derive_host_key_alias(NODE_ID)
    _write_managed_entry(cfg, alias)
    probe = _probe(keyscan_stdout=f"agdnsmasq.local ssh-ed25519 {KEY_BLOB}\n")

    entries = verify_offered_keys(cfg, ["agdnsmasq"], _snapshot(), probe)

    assert entries[0].status == STATUS_READY


def test_verify_offered_keys_bootstrap_mode_unreachable_detail_when_no_mdns(tmp_path):
    cfg = _config(tmp_path)
    alias = derive_host_key_alias(NODE_ID)
    _write_managed_entry(cfg, alias)
    node = DesiredNode(id=NODE_ID, slug="agdnsmasq", name="agdnsmasq", lifecycle="active", node_type="device")
    snapshot = DesiredSnapshot(nodes=[node])  # no endpoints at all
    probe = _probe(keyscan_raises=AssertionError("should not be called"))

    entries = verify_offered_keys(cfg, ["agdnsmasq"], snapshot, probe)

    assert entries[0].status == STATUS_UNREACHABLE
    assert entries[0].detail == "no_resolvable_route"
