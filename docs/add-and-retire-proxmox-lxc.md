# Adding and retiring a Proxmox LXC guest

## Guest OS and compute are separate realization layers

For a compute-backed guest, the Nautobot **Device** represents the managed guest OS: it is the
`DesiredNode.realized_device` target and owns nodeutils facts, observed services, operational
derivation, and node-level IPAM evidence. The Nautobot **VirtualMachine** represents the Proxmox
compute resource: it is the `DesiredComputeInstance.realized_vm` target and owns VMID, guest
kind, capacity, power, interfaces, and Proxmox realization. Both objects may legitimately
describe one guest; a VirtualMachine never replaces the Device link for guest-OS realization.
See [`../../devdocs/big/vm/roadmap.md`](../../devdocs/big/vm/roadmap.md) for the detailed contract.

This is the ordinary, bounded workflow for an approved LXC guest; it is not a generic Proxmox
lifecycle interface.

## Adding one Proxmox LXC guest

### Canonical desired-state batch

Register the Proxmox platform and its control node first. Then use this one batch shape for one
actionable LXC (replace the example names, addresses, VMID, template, storage, bridge, and
platform slug with the approved values for the target):

```yaml
dry_run: true
operations:
  - op: upsert
    kind: desired_node
    key: {slug: aglxc01}
    values:
      name: aglxc01
      # The DesiredNode represents the guest OS Device. The LXC itself is
      # represented by DesiredComputeInstance below.
      node_type: service_host
      accepted_actual_types:
        - device
      lifecycle: active
  - op: upsert
    kind: desired_endpoint
    key: {desired_node: aglxc01, name: primary, endpoint_type: primary}
    values:
      ip_policy: static
      ip_address: 192.168.50.101/24
      gateway_address: 192.168.50.1
      mac_address: "02:00:00:00:00:01"
      mdns_name: aglxc01.local
  - op: upsert
    kind: desired_compute_instance
    key: {desired_node: aglxc01}
    values:
      platform: pve-main
      instance_kind: container
      desired_power_state: running
      vcpus: 2
      memory_mb: 2048
      root_disk_gb: 16
      config:
        vmid: 101
        template: local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst
        storage: local-lvm
        bridge: vmbr0
        unprivileged: true
```

Preview the exact batch first, then commit that same file atomically:

```bash
uv run --project nctl nctl desired apply -f .local/aglxc01.yaml --json
uv run --project nctl nctl desired apply -f .local/aglxc01.yaml --yes --json
```

`planned`, `deprecated`, and `retired` intent is not creation-ready, so it does not require this
complete actionable endpoint contract. Preview reports the batch actions only; final Django model
validation runs during the atomic apply. On a rejected apply, nctl prints the server transaction
error and per-operation conflict reasons (or the complete server artifact with `--json`).

1. Record a user-confirmed Braindump wish, then write the exact desired node, one primary endpoint
   (static IPv4 CIDR, same-subnet gateway, and explicit MAC), compute platform, VMID, LXC template, rootfs storage, bridge,
   resources, and desired running state through nintent's canonical writer. A prose wish alone
   cannot plan or create a guest.
2. Run `nctl reconcile GUEST --json` without `--yes`. The plan must name only that guest and its
   pinned control host. Its preflight requires fresh platform evidence, the exact template,
   storage, and bridge, one NIC-bearing endpoint, and no desired or actual VMID/MAC/IP collision.
   A missing template, collision, stale ledger, ambiguous endpoint, or unreachable platform is a
   stop to correct or refresh evidence; do not bypass it with direct `pct` commands.
3. After reviewing the unchanged plan, run `nctl reconcile GUEST --yes` under separate apply
   authority. The registered handler uses the existing strict SSH trust and only the bounded
   `ansible_agdev/playbooks/proxmox/create_lxc.yml` adapter (`pct status`, `pct create`, and
   `pct start`). It cannot stop, delete, resize, replace, migrate, clone, or create QEMU guests.
4. Reconcile re-observes and ingests the guest, then links the exact realized VirtualMachine. If
   creation succeeded but observation or identification failed, treat the operation evidence as
   partial progress: find the recorded VMID, refresh observation, and link the identified guest.
   Never submit a second create as recovery.
5. A newly linked LXC without guest OS access reaches
   `waiting_for_manual_initial_access`. The static IPv4 CIDR and gateway are already configured by
   `pct create`; use the Proxmox console for guest user, key, privilege, SSH, and mDNS setup;
   complete normal node observation and enrollment afterward.
   Until then it is intentionally excluded from production inventory. A repeat dry plan must not
   create, start, or link it again.

## Retiring one Proxmox LXC

Braindump text and its supersession status are never reconciliation input. After the user confirms
the exact target, submit one canonical desired-state batch that sets the owning `DesiredNode` to
`retired` and that `DesiredComputeInstance.desired_presence` to `absent`. Do not treat an omitted
Desired row, an unmanaged guest, or a missing observation as deletion intent.

For an existing guest, the smallest canonical document is:

```yaml
dry_run: true
operations:
  - op: upsert
    kind: desired_node
    key: {slug: GUEST}
    values: {lifecycle: retired}
  - op: upsert
    kind: desired_compute_instance
    key: {desired_node: GUEST}
    values: {desired_presence: absent}
```

Preview it with `nctl desired apply -f RETIREMENT.yaml`; after review, use the
same document with `--yes`. These partial upserts preserve every omitted field
on the existing node and compute instance. This is a retirement update, not a
generic VM lifecycle API.

1. Run `nctl reconcile GUEST --allow-destroy --json` without `--yes` and review the one pinned
   `data.plan.actions` entry with `reconciler_id: destroy_compute_instance`: its target slug,
   `evidence.vmid`, and `evidence.control_desired_node_slug` must name the expected guest, LXC VMID, and
   exact Proxmox control node. `data.plan_path` names the identical durable plan artifact.
2. `nctl reconcile GUEST --allow-destroy` remains a dry plan. `nctl reconcile GUEST --yes` refuses
   the action with `destroy_capability_not_enabled`; neither command reaches Proxmox.
3. Only after reviewing the same target, run `nctl reconcile GUEST --allow-destroy --yes`. The
   handler re-derives the disposition before mutation and invokes only the bounded
   `ansible_agdev/playbooks/proxmox/destroy_lxc.yml` adapter for that VMID and control node.
4. Reconcile performs its normal control-node observation and Nautobot ingest. It completes only
   when fresh drift observes the retained VirtualMachine with `proxmox_presence=absent`. If
   destruction succeeded but that observation fails, retain the operation evidence and refresh
   observation; do not submit a second destroy blindly.

This removes only the planned LXC. `compute_instance_removal_complete` in fresh drift is successful
removal evidence, so this reconcile ends `converged` with `ok: true`; unresolved or ambiguous
removal evidence remains non-successful. After this converged state has been reviewed, `nctl prune
GUEST` shows the separate exact-host ledger cleanup and `nctl prune GUEST --yes` deletes every
surviving linked Actual root before the Desired tombstones. The linked VirtualMachine and Device are
independent roots: a guest without an initial nodeutils observation has no Device, so its retained
VirtualMachine is still planned and deleted by itself. A retry similarly plans only roots still
present; it never drops Desired tombstones while a known linked Actual root remains. Prune does not contact Proxmox or
Ansible, retains Braindumps and prior operation evidence, and does not support QEMU, wildcard
targets, schedules, or general provider disposal.

See also the `retire-proxmox-lxc` Claude Code skill (`.claude/skills/retire-proxmox-lxc/`) for an
enumerated manual-review branch table covering this retirement flow.
