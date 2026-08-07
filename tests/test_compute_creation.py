"""Kind-aware guest-create derivation: LXC and QEMU paths stay explicitly separated."""

from __future__ import annotations

from datetime import datetime, timezone

from nctl_core.compute.model import DesiredComputeInstance, DesiredComputePlatform
from nctl_core.drift.compute_creation import derive_compute_creations
from nctl_core.sources.actual import ActualCluster, ActualDevice, ActualSnapshot, ActualVMInterface
from nctl_core.sources.desired import DesiredEndpoint, DesiredNode, DesiredSnapshot
from nctl_core.sources.snapshot import SourceSnapshot

GENERATED_AT = "2026-08-07T00:00:00+00:00"

ISO_VOLID = "local:iso/ubuntu-24.04.2-live-server-amd64.iso"
VZTMPL_VOLID = "local:vztmpl/debian-12.tar.zst"


def _endpoint(*, static: bool) -> DesiredEndpoint:
    address = {"ip_address": "192.0.2.9/24", "gateway_address": "192.0.2.1"} if static else {"ip_address": "192.0.2.9/24"}
    policy = {"ip_policy": "static"} if static else {"ip_policy": "dhcp_reserved", "dns_name": "agguest.example", "generate_dnsmasq": True}
    return DesiredEndpoint(
        id="ep-1", name="primary", endpoint_type="primary", node_id="guest-node", node_slug="agguest",
        mdns_name="agguest.local", mac_address="bc:24:11:00:01:09", **address, **policy,
    )


def _snapshot(*, instance_kind: str, template: str, static_endpoint: bool) -> SourceSnapshot:
    control = DesiredNode(id="control-node", slug="aghub", name="aghub", lifecycle="active", node_type="device", realized_device_id="dev-1")
    guest = DesiredNode(id="guest-node", slug="agguest", name="agguest", lifecycle="active", node_type="vm")
    platform = DesiredComputePlatform(
        id="platform-1", name="aghub-pve", slug="aghub-pve", provider_type="proxmox", lifecycle="active",
        control_node_id="control-node", config_schema_version="v1", config={"cluster_name": "aghub"},
    )
    config = {"vmid": 209, "template": template, "storage": "local-lvm", "bridge": "vmbr0"}
    if instance_kind == "container":
        config["unprivileged"] = True
    instance = DesiredComputeInstance(
        id="instance-1", desired_node_id="guest-node", platform_id="platform-1", instance_kind=instance_kind,
        vcpus=2, memory_mb=2048, root_disk_gb=32, config_schema_version="v1", config=config,
    )
    cluster = ActualCluster(id="cluster-1", name="aghub", proxmox={
        "observer_device_id": "dev-1", "observed_at": GENERATED_AT, "observation_state": "complete",
        "observed_node_names": ["aghub"],
        "storage_content": {
            "aghub/local/iso": {"node": "aghub", "storage": "local", "content_type": "iso", "state": "complete", "items": [{"volid": ISO_VOLID}]},
            "aghub/local/vztmpl": {"node": "aghub", "storage": "local", "content_type": "vztmpl", "state": "complete", "items": [{"volid": VZTMPL_VOLID}]},
            "aghub/local-lvm/rootdir": {"node": "aghub", "storage": "local-lvm", "content_type": "rootdir", "state": "complete", "items": []},
        },
    })
    return SourceSnapshot(
        desired=DesiredSnapshot(nodes=[control, guest], endpoints=[_endpoint(static=static_endpoint)], compute_platforms=[platform], compute_instances=[instance]),
        actual=ActualSnapshot(
            devices=[ActualDevice(id="dev-1", name="aghub.local")],
            clusters=[cluster],
            vm_interfaces=[ActualVMInterface(id="iface-1", name="net0", proxmox={"bridge": "vmbr0"})],
        ),
        fetched_at=datetime.now(timezone.utc),
    )


def test_container_intent_derives_a_pinned_lxc_creation():
    creation = derive_compute_creations(_snapshot(instance_kind="container", template=VZTMPL_VOLID, static_endpoint=True), generated_at=GENERATED_AT)["instance-1"]
    assert creation.failures == ()
    assert creation.parameters["guest_type"] == "lxc"
    assert creation.parameters["unprivileged"] is True
    assert creation.parameters["ipv4_cidr"] == "192.0.2.9/24"
    assert creation.parameters["gateway_ipv4"] == "192.0.2.1"


def test_virtual_machine_intent_derives_a_pinned_qemu_creation_without_lxc_only_parameters():
    creation = derive_compute_creations(_snapshot(instance_kind="virtual_machine", template=ISO_VOLID, static_endpoint=False), generated_at=GENERATED_AT)["instance-1"]
    assert creation.failures == ()
    assert creation.parameters["guest_type"] == "qemu"
    assert creation.parameters["template"] == ISO_VOLID
    for lxc_only in ("unprivileged", "ipv4_cidr", "gateway_ipv4"):
        assert lxc_only not in creation.parameters


def test_virtual_machine_with_container_template_fails_the_iso_gate_instead_of_creating_an_lxc():
    """The historical gap: virtual_machine intent + vztmpl volid must never route to `pct create`."""
    creation = derive_compute_creations(_snapshot(instance_kind="virtual_machine", template=VZTMPL_VOLID, static_endpoint=False), generated_at=GENERATED_AT)["instance-1"]
    assert creation.parameters["guest_type"] == "qemu"
    assert [code for code, *_ in creation.failures] == ["compute_template_unavailable"]


def test_container_with_dhcp_endpoint_still_requires_a_static_network_but_a_virtual_machine_does_not():
    container = derive_compute_creations(_snapshot(instance_kind="container", template=VZTMPL_VOLID, static_endpoint=False), generated_at=GENERATED_AT)["instance-1"]
    assert "compute_endpoint_network_incomplete" in [code for code, *_ in container.failures]
    vm = derive_compute_creations(_snapshot(instance_kind="virtual_machine", template=ISO_VOLID, static_endpoint=False), generated_at=GENERATED_AT)["instance-1"]
    assert "compute_endpoint_network_incomplete" not in [code for code, *_ in vm.failures]
