"""The narrow post-create state before a guest has guest-OS observation."""

from __future__ import annotations


def awaiting_manual_initial_access(snapshot, node) -> bool:
    """True only for a linked, running guest with no device or nodeutils facts.

    This is deliberately a terminal state, not a relaxed actual-state policy:
    the first nodeutils observation (or any missing condition) returns the
    node to the ordinary evaluation path.
    """
    if node.realized_device_id:
        return False
    observed_names = {item.hostname.casefold() for item in snapshot.observed}
    if {node.slug.casefold(), f"{node.slug}.local"} & observed_names:
        return False
    vms = {item.id: item for item in snapshot.actual.virtual_machines}
    return any(
        instance.desired_node_id == node.id
        and instance.realized_vm_id in vms
        and vms[instance.realized_vm_id].proxmox is not None
        and vms[instance.realized_vm_id].proxmox.status == "running"
        for instance in snapshot.desired.compute_instances
    )
