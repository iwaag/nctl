from nctl_core.drift.compute_disposition import derive_compute_dispositions
from nctl_core.drift.compute_realization import derive_compute_realizations
from test_compute_evaluation import NOW, _snapshot


def _disposition(**kwargs):
    return derive_compute_dispositions(_snapshot(**kwargs), generated_at=NOW)["instance"]


def test_disposition_is_total_and_covers_primary_outcomes():
    ordinary = _disposition()
    assert ordinary.outcome == "ordinary"

    retired = _snapshot(link=True)
    retired.desired.nodes[0] = retired.desired.nodes[0].model_copy(update={"lifecycle": "retired"})
    retired.desired.compute_instances[0] = retired.desired.compute_instances[0].model_copy(update={"desired_presence": "present"})
    assert derive_compute_dispositions(retired, generated_at=NOW)["instance"].outcome == "retained"

    absent = retired.model_copy(deep=True)
    absent.desired.compute_instances[0] = absent.desired.compute_instances[0].model_copy(update={"desired_presence": "absent"})
    assert derive_compute_dispositions(absent, generated_at=NOW)["instance"].outcome == "retained"

    present = absent.model_copy(deep=True)
    present.actual.virtual_machines[0] = present.actual.virtual_machines[0].model_copy(update={"proxmox": present.actual.virtual_machines[0].proxmox.model_copy(update={"presence": "present"})})
    disposition = derive_compute_dispositions(present, generated_at=NOW)["instance"]
    assert disposition.outcome == "destroy_required"
    assert disposition.parameters["host_slugs"] == ["guest"]

    removed = present.model_copy(deep=True)
    removed.actual.virtual_machines[0] = removed.actual.virtual_machines[0].model_copy(update={"proxmox": removed.actual.virtual_machines[0].proxmox.model_copy(update={"presence": "absent"})})
    assert derive_compute_dispositions(removed, generated_at=NOW)["instance"].outcome == "removal_complete"

    conflict = _snapshot(link=True)
    conflict.desired.compute_instances[0] = conflict.desired.compute_instances[0].model_copy(update={"desired_presence": "absent"})
    assert derive_compute_dispositions(conflict, generated_at=NOW)["instance"].outcome == "presence_conflict"

    stale = _snapshot(cluster={"proxmox": {"observer_device_id": "device", "observed_at": "2026-07-20T00:00:00+00:00", "observation_state": "complete", "observed_node_names": ["host"]}}, link=True)
    assert derive_compute_dispositions(stale, generated_at=NOW)["instance"].outcome == "unknown"


def test_destroy_gate_rejections_are_named():
    base = _snapshot(link=True)
    base.desired.nodes[0] = base.desired.nodes[0].model_copy(update={"lifecycle": "retired"})
    base.desired.compute_instances[0] = base.desired.compute_instances[0].model_copy(update={"desired_presence": "absent"})
    base.actual.virtual_machines[0] = base.actual.virtual_machines[0].model_copy(update={"proxmox": base.actual.virtual_machines[0].proxmox.model_copy(update={"presence": "present"})})
    cases = [
        ("guest_type_disagrees_with_instance_kind", lambda s: s.actual.virtual_machines.__setitem__(0, s.actual.virtual_machines[0].model_copy(update={"proxmox": s.actual.virtual_machines[0].proxmox.model_copy(update={"guest_type": "qemu"})}))),
        ("vmid_disagrees", lambda s: s.actual.virtual_machines.__setitem__(0, s.actual.virtual_machines[0].model_copy(update={"proxmox": s.actual.virtual_machines[0].proxmox.model_copy(update={"vmid": 999})}))),
    ]
    for expected, change in cases:
        snapshot = base.model_copy(deep=True)
        change(snapshot)
        disposition = derive_compute_dispositions(snapshot, generated_at=NOW)["instance"]
        assert disposition.outcome == "retained"
        assert disposition.gate_failure == expected

    assert set(derive_compute_dispositions(base, generated_at=NOW)) == {item.id for item in base.desired.compute_instances}
    assert derive_compute_realizations(base, generated_at=NOW)["instance"].virtual_machine is not None


def test_qemu_virtual_machine_can_be_destroy_required():
    base = _snapshot(link=True)
    base.desired.nodes[0] = base.desired.nodes[0].model_copy(update={"lifecycle": "retired"})
    base.desired.compute_instances[0] = base.desired.compute_instances[0].model_copy(
        update={"desired_presence": "absent", "instance_kind": "virtual_machine"}
    )
    base.actual.virtual_machines[0] = base.actual.virtual_machines[0].model_copy(update={
        "proxmox": base.actual.virtual_machines[0].proxmox.model_copy(update={"presence": "present", "guest_type": "qemu"})
    })
    disposition = derive_compute_dispositions(base, generated_at=NOW)["instance"]
    assert disposition.outcome == "destroy_required"
    assert disposition.parameters["guest_type"] == "qemu"

    mismatched = base.model_copy(deep=True)
    mismatched.actual.virtual_machines[0] = mismatched.actual.virtual_machines[0].model_copy(update={
        "proxmox": mismatched.actual.virtual_machines[0].proxmox.model_copy(update={"guest_type": "lxc"})
    })
    mismatched_disposition = derive_compute_dispositions(mismatched, generated_at=NOW)["instance"]
    assert mismatched_disposition.outcome == "retained"
    assert mismatched_disposition.gate_failure == "guest_type_disagrees_with_instance_kind"
