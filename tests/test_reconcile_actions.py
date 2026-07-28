"""Handler-owned reconcile action contracts."""

from datetime import datetime, timezone
from types import SimpleNamespace

from nctl_core.drift.model import Target
from nctl_core.reconcile.actions import playbook as playbook_module
from nctl_core.reconcile.model import ReconcileAction
from nctl_core.sources.actual import ActualDevice, ActualSnapshot
from nctl_core.sources.desired import DesiredSnapshot, DesiredNode
from nctl_core.sources.snapshot import SourceSnapshot


def test_playbook_grouping_passes_the_fixed_operation_timestamp_to_resolver(monkeypatch):
    node = DesiredNode(
        id="11111111-1111-1111-1111-111111111111", slug="agweb", name="agweb",
        lifecycle="active", node_type="device", accepted_actual_types=["device"],
        realized_device_id="dev-1",
    )
    snapshot = SourceSnapshot(
        desired=DesiredSnapshot(nodes=[node]),
        actual=ActualSnapshot(devices=[ActualDevice(id="dev-1", name="agweb.local")]),
        fetched_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    action = ReconcileAction(
        id="service_profile:web", reconciler_id="service_profile", action_kind="playbook",
        targets=[Target(kind="service", slug="web", id="s1")],
        claimed_diff_codes=["service_not_running"], reason="test", mutates=True,
        requires_observation=False, parameters={"playbook_by_os": {"linux": "playbooks/linux.yml"}},
    )
    seen = {}

    def fake_resolve(**kwargs):
        seen["generated_at"] = kwargs["generated_at"]
        return SimpleNamespace(host_os=SimpleNamespace(value="linux"))

    monkeypatch.setattr(playbook_module, "resolve_operational_values", fake_resolve)
    groups = playbook_module._group_hosts_by_playbook(
        action, ["agweb"], snapshot, generated_at="2026-07-20T12:34:56+00:00"
    )
    assert groups == {"playbooks/linux.yml": ["agweb"]}
    assert seen["generated_at"] == "2026-07-20T12:34:56+00:00"
