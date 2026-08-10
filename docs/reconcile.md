# `reconcile`

See [`../README.md`](../README.md#usage) for the full command list.

`nctl reconcile [HOST] [--yes] [--max-rounds N] [--json]` is the routine, single-command path from
drift to a freshly verified converged state — the AI-exception-handler model from the roadmap
depends on this being the normal way anything (human, cron, or AI) drives convergence, reading
drift/event artifacts only when it stops short of `converged`.

- **Plan mode** (no `--yes`, the default): builds one full-cluster drift, projects the requested
  scope (a desired-node slug, or the whole cluster with no argument), and persists a plan without
  touching the ledger, Ansible, or Nautobot Jobs. Exit 0 whenever planning itself succeeds
  (`state: planned`), even if the plan describes real drift — a dry plan is not expected to be
  clean.
- **Apply mode** (`--yes`): executes the plan's actions in dependency order, across up to
  `--max-rounds` bounded re-plan rounds (overrides `[reconcile].max_rounds`, clamped to `1..10` by
  the CLI itself as a usage error). Each round re-fetches one fresh full-cluster drift, runs
  bootstrap/ledger actions (nodeutils collection + Nautobot ingest, unique actual-node linking,
  scoped IPAM), atomically regenerates the **full** production inventory (even for a host-scoped
  run, so a partial document never replaces the canonical one), then service/dnsmasq playbook
  actions, then re-observes any host that needed it. A round with an empty plan and no remaining
  automatic maintenance action is `already_converged`/`converged`; an unchanged drift fingerprint
  between rounds is `non_converged` (`no_progress`); exhausting `--max-rounds` without converging
  is also `non_converged` (`max_rounds_reached`); any manual/unsupported plan finding stops the run
  **before any mutation** (`manual_intervention_required`). Informational completion evidence such
  as `compute_instance_removal_complete` remains visible in drift and artifacts but does not block a
  successful terminal state; a controller-local lock held by another
  reconcile fails immediately (`reconcile_lock_contention`) before the first drift fetch. Exit 0
  only for `already_converged`/`converged`; every other apply-mode state exits 1.
- **Scope**: an independent target's failure never blocks other independent targets in the same
  scope — the run still reports the overall result as non-`converged` if any selected target never
  reaches a fresh `converged` status, but reachable/healthy targets still make progress. A host
  argument must resolve to exactly one desired-node slug; zero or multiple matches are a usage
  error (exit 2), not a run failure.
- **Audit trail**: before `--yes` mutates anything, nctl verifies the operation directory and event
  log are writable and refuses to proceed if they aren't (`artifact_write_failed`) — a mutating run
  never proceeds without a place to record what it did.

The `nctl.reconcile.v2` envelope's `data` carries `operation_id`, `mode`, `scope`, terminal `state`
(`planned | already_converged | converged | manual_intervention_required | non_converged | failed`),
`event_log_path`, `artifact_dir`, `plan_path`, plan-mode `plan`, initial/final drift paths, per-round action results
(`rounds`), `manual_review`/`unsupported` records (target + diff code + evidence), scope/global
status summaries, and `ssh_preflight` (below). In plan mode, `data.plan` is the complete
`nctl.reconcile.plan.v1` object as well as being persisted at `data.plan_path`; agents can inspect
`data.plan.actions` directly without opening logs. It never contains a Nautobot token, raw report content,
or arbitrary shell text — actions carry typed parameters and claimed diff codes, not prose. Neither
`plan.json` nor `result.json` are deleted on failure: a non-`converged` run leaves its full operation
directory (`round-NN/drift-*.json`, `round-NN/ansible/*.std{out,err}`, `round-NN/jobs/*.json`,
`round-NN/reports/*.json`, `round-NN/probe-config/*.yaml`) behind for AI or human diagnosis, per the
roadmap's "AI reads these to diagnose" model. Report/config/job artifacts are written mode `0600`;
directories `0700`.

```toml
[reconcile]
max_rounds = 3                                  # 1..10, overridable per run with --max-rounds
job_poll_interval_seconds = 2.0
job_timeout_seconds = 300.0
ansible_timeout_seconds = 1800.0
remote_report_path = "/var/lib/nodeutils/inventory.json"  # must be absolute
max_report_bytes = 2097152
max_report_age_hours = 72
ingest_policy_file = "seed/nodeutils_ingest.yaml"
service_observation_max_age_hours = 24
lock_path = "~/.local/state/nctl/reconcile.lock"
# Source checkouts normally omit this: nctl resolves the superproject's
# pinned nodeutils gitlink. Packaged controllers may set the same full SHA.
# nodeutils_version = "0123456789abcdef0123456789abcdef01234567"
```

Every observation passes an exact `nodeutils_version` commit to Ansible. In a
normal source checkout this is the `nodeutils` gitlink recorded by the
superproject commit, not the mutable GitHub `HEAD` and not whatever commit
happens to be checked out in the submodule working tree. This keeps the remote
collector schema coordinated with the local nctl reader. The resolved SHA is
recorded in the operation event log and observation action result. If the
superproject gitlink cannot be resolved, observation fails before Ansible
runs; it never falls back to `HEAD`.

**SSH trust preflight** (`devdocs/small/fix_sshkey/plan.md` Step 5): every round, before observation,
Nautobot Jobs, inventory writes, or playbooks run, nctl checks that every node touched by an
SSH-requiring action has at least one entry under its stable alias in `[ssh].known_hosts_file`.
Which reconcilers require SSH, and which hosts one action touches, both have a single
machine-readable home (no_guest_vm Step 3): the reconciler registry's `connects_over_ssh` flag
derives the requiring set (`observe_node`, `create_compute_instance`, `destroy_compute_instance`,
`service_profile`, `dnsmasq_config`), and every such action pins its exact contacted hosts in
`parameters["host_slugs"]` — compute create/destroy gate on their **control node**, never the guest.
`tests/test_reconcile_ssh_invariant.py` executes each handler against fakes and asserts the declared
hosts equal the hosts actually contacted. A missing entry fails the whole round with
`ssh_host_key_unenrolled` and the verified-source `nctl ssh enroll <slug>
--from-known-hosts`/`--fingerprint` remediation, before any write --
this is what prevented the original incident (`agdnsmasq`'s observation/IPAM succeeding, then
failing on the production dnsmasq SSH connection). Ledger-only actions (`link_actual_node`,
`link_compute_realization`, `reconcile_ipam`) never require enrollment, so an unrelated unenrolled
host never blocks them. See [ssh-trust.md](ssh-trust.md) for the full SSH trust store contract.

**Compute evidence routing and retired nodes** (no_guest_vm Steps 1-2): a scoped compute instance
with no realized VirtualMachine and no plannable create action gets its evidence refresh planned as
an `observe_node` on its platform's **control node** (`observe_node:compute-evidence`) — the
hypervisor-side collection/ingest is the linking path, so a created-then-orphaned guest is
recoverable from its own scope without the guest ever answering SSH (see
[add-and-retire-proxmox-lxc.md](add-and-retire-proxmox-lxc.md) §Recovering a created-then-orphaned
guest). This deliberately actuates a node outside the requested host scope; it is a read-only
collection. A node whose effective lifecycle is `retired` is never planned as an `observe_node`
target at all — its evidence gaps stay visible in drift instead of producing an SSH-gated action
that can only fail. The same suppression applies to an *active* compute guest that has no realized
VirtualMachine **and** no realized Device (no_guest_vm G3): it has never run sshd, so a
guest-targeted `observe_node` could only fail `ssh_host_key_unenrolled`; its refresh routes to the
control node instead. A guest that does have a realized Device keeps its own observe action — it
exists and is enrolled; only its facts are stale.

**Retired guests link before they destroy** (no_guest_vm G2): a retired instance whose VM matched
only by vmid/name still surfaces `compute_instance_not_linked`, so the plan records the realization
link in the same round as — and ordered ahead of, via an action dependency — the
`destroy_compute_instance`. The destroy therefore always operates on a linked row, and
`nctl prune` (whose server-side Actual cleanup accepts only Desired-linked roots, never a
vmid/name search) can collect the VirtualMachine record. This also closes the tombstone trap: an
unlinked VM row destroyed and left behind used to re-bind by vmid/name to any later re-declared
node of the same identity. An already-absent unlinked row (an existing tombstone) is cleaned the
same way — re-declare the guest retired/absent, `nctl reconcile GUEST --yes` plans just the link,
then `nctl prune GUEST --yes` collects it.

**IPAM eligibility is policy-aware, not `ip_policy=dhcp_reserved`-only** (ipam_policy plan): an
endpoint with a `dhcp_reserved` IP is always automatic (`missing_actual_ip_address`/
`actual_ip_address_not_linked` -> `reconcile_ipam`), matching prior behavior. A `static`/`external`
endpoint is automatic only when its linked realized Device's observed `primary_ip_address` matches
the desired host; otherwise drift reports one of `ipam_reconcile_observation_missing`/`_mismatch`/
`_ambiguous` as a `manual_review` finding instead of silently repeating the same unresolved action
every round. IPAM ledger reconciliation only creates/links the Nautobot `IPAddress` object when
both an explicit desired IP and a matching self-observation already exist; it never actuates the
host's own IP configuration and never assigns an `IPAddress` to an `Interface`.
Presence in the trust store is re-verified against what a route currently offers, at two points:
`observe_node` targets are scanned over mDNS before the bootstrap phase, and `service_profile`/
`dnsmasq_config` targets are scanned again after production inventory regeneration, this time over
whichever route `nctl render production`'s own connection resolution (`local_ip -> dns -> mdns ->
inventory_hostname`) actually selected (`production.composer.resolve_effective_route`, shared by both
so preflight never runs a second, disagreeing route-selection implementation). A mismatch or
unreachable route fails with `ssh_host_key_mismatch`/`ssh_host_key_unreachable` before the first
playbook using that route runs. A scan can only prove a mismatch against an already-trusted
key -- it never authorizes a new one. The per-host `ready`/`unenrolled` result is always surfaced in
`data.ssh_preflight`, even in a dry plan (where it is informational only, no scan runs, and it never
blocks). OpenSSH itself, with `StrictHostKeyChecking=yes` and `HostKeyAlias`, remains the final
verifier of the actual connection.

`nctl reconcile --yes` is the routine entry point that replaces the old
`bootstrap-inventory` → `collect_nodeutils_and_ingest_nautobot.yml` → `production-inventory`
Ansible/Makefile sequence; `ansible_agdev/Makefile`'s `pipeline` target now runs exactly this
command. `make bootstrap-inventory`/`make production-inventory` remain as standalone diagnostics —
`reconcile` renders its own operation-scoped bootstrap inventory and regenerates the full production
inventory itself, so it never shells out to either.

`nctl reconcile HOST --refresh-observation` adds one explicit `observe_node`
action to the first round even when the current drift is already converged.
Without `--yes` it is a dry plan; with `--yes` it updates nodeutils to the
superproject-pinned commit, collects and ingests one fresh report, then returns
to the ordinary round planner so the refresh is not repeated indefinitely.
Host scope is required to prevent an accidental whole-cluster observation.
