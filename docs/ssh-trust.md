# SSH trust configuration

See [`../README.md`](../README.md#ssh-trust-configuration) for the config block quick reference.

```toml
[ssh]
known_hosts_file = "~/.local/state/nctl/ssh/known_hosts" # default
keyscan_timeout_seconds = 10                              # default
lock_path = "~/.local/state/nctl/ssh.lock"                # default
```

`known_hosts_file` is a dedicated, nctl-managed known_hosts store keyed by the stable
`nctl-node-<DesiredNode UUID>` `HostKeyAlias` (see `devdocs/small/fix_sshkey/plan.md` and
`devdocs/small/fix_sshkey2/plan.md`), not a credential and not a generated repo artifact: it is
never committed, copied into an operation artifact, or written to Nautobot/nintent. `[ssh]` is
optional; all three keys default as shown when the section is absent. `known_hosts_file`/`lock_path`
resolve relative to `nctl.toml`'s own directory, not the process working directory, so enrollment,
inventory rendering, preflight, and `apply dnsmasq` always agree on the same absolute path
regardless of where a command happens to run from.

The managed store's key is always the bare alias, independent of `ansible_port`: OpenSSH ignores
the connection port entirely once `HostKeyAlias` is set, so a non-default-port node (e.g.
`ansible_port = 2222`) is looked up exactly the same way as one on port 22. Only *legacy*
known_hosts promotion (`nctl ssh enroll --from-known-hosts`, searching your ordinary OpenSSH
known_hosts files) ever uses a port-qualified `[host]:port` name, and only for that search -- never
for the managed store itself.

The store has one strict reader (`devdocs/small/fix_sshkey4/plan.md`): an absent file is a valid
empty store (`unenrolled` for every host), and every other non-blank/non-comment line must be a
supported bare-alias entry or it is corruption. A missing enrollment and an invalid managed store
are always distinguishable -- corruption never falls back to `unenrolled` -- and every public
boundary (`ssh enroll`, `apply dnsmasq`, reconcile's pre-round gate, bootstrap and post-actuation
observation inside a started round) reports it as a structured `ssh_store_read_failed` result
rather than an uncaught exception. A store failure that occurs after a round has already recorded
progress never discards that progress: the round, its completed actions, and a freshly refreshed
final drift are retained, with `final_drift_unknown` recorded if even that refresh fails. A
syntactically valid historical `[nctl-node-UUID]:port` entry is recognized separately as migration
residue -- it never satisfies current enrollment and is removed only by a subsequent verified
enrollment/replacement write, never automatically.

## Lifecycle

```text
discover by mDNS
  -> verify fingerprint / promote existing trusted .local key
  -> nctl ssh enroll
  -> observe and reconcile IPAM/DNS/DHCP
  -> connect by DNS/IP/Tailscale under the same HostKeyAlias
```

A node is first reached as `<hostname>.local` over mDNS. Before enrolling, its offered key must be
backed by one of two verified sources -- an unverified `ssh-keyscan` result is never sufficient on
its own, even with `--yes`:

- `nctl ssh enroll <slug> --from-known-hosts` -- promotes an already-trusted `.local` entry from
  your (or the operator's) ordinary OpenSSH user known_hosts files. This is the migration path for
  a cluster that already has trusted `.local` entries from manual `ssh` use.
- `nctl ssh enroll <slug> --fingerprint SHA256:...` -- the clean path for a brand-new machine with
  no prior entry. The fingerprint must come from a trusted out-of-band channel (machine console,
  provisioning output, an administrator reading it off the device) -- never from an unverified
  network scan. Repeat `--fingerprint` if you deliberately pin more than one key algorithm.

Once enrolled, `nctl reconcile --yes` observes the node and reconciles IPAM/DNS/DHCP; from then on,
bootstrap inventory, production inventory, `apply dnsmasq`, and direct `ansible-playbook`/`ansible`
invocations against either generated inventory all connect under the identical `HostKeyAlias` no
matter which of `.local`, `.home.arpa`, a reserved/static IP, or a Tailscale address `ansible_host`
currently resolves to. Changing only the endpoint never requires another enrollment and never adds
an endpoint-keyed trust entry.

## Hardware replacement and key rotation

Reusing a DesiredNode slot for replacement hardware intentionally produces a key mismatch --
`ssh_host_key_conflict` from `nctl ssh enroll`, or `ssh_host_key_mismatch` from `nctl reconcile
--yes`'s preflight -- rather than silently inheriting trust because the new machine acquired the
old IP or DNS name. To knowingly replace the key for an existing alias:

```bash
nctl ssh enroll <slug> --replace --fingerprint SHA256:<new-machine's-verified-fingerprint> --yes
```

`--replace` requires **all** of: `--replace` itself, a verified source (`--from-known-hosts` or a
matching `--fingerprint`), and `--yes`. Only the exact managed alias entry changes; unrelated
entries and comments in the managed file are preserved untouched.

## Recovering from a lost or corrupted managed file

If `[ssh].known_hosts_file` is lost, corrupted, or reset, the only supported recovery is
re-enrolling each node (`nctl ssh enroll <slug> --from-known-hosts` or `--fingerprint ...`, per
node, through the same verified-source rules above). Do not "fix" a missing/broken managed file by
setting `StrictHostKeyChecking=no`, using `accept-new`, or copying in an unverified `ssh-keyscan`
result -- none of those are a substitute for a verified source, and all of them defeat the fail-closed
guarantee this store exists to provide.

## Direct Ansible use

Both generated inventories (`hosts_intent.yml` and `production.yml`) carry the same closed, strict
host variables (`nctl_ssh_host_key_alias`, `ansible_ssh_common_args` with
`StrictHostKeyChecking=yes`), so a direct `ansible`/`ansible-playbook` invocation against either one
fails closed exactly like `nctl` does, just with OpenSSH's generic `Host key verification failed`
instead of a structured nctl error code. A hand-written or otherwise-sourced inventory that lacks
these variables is outside the supported operational path: `nctl apply dnsmasq` rejects one
(`dnsmasq_inventory_untrusted_host`) rather than silently falling back to endpoint-keyed
verification -- for the normally configured inventory exactly as much as an explicit `--inventory`
override when `--yes` is requested (fix_sshkey2 Step 4; before that fix, only
`--inventory` was checked, and only for alias/node-ID presence). Passing the variable check is not
enough on its own either: `apply dnsmasq` also re-scans the route resolved from the inventory's own
host vars and requires the currently offered key to match a managed entry before Ansible starts,
failing closed with `ssh_host_key_unenrolled`/`ssh_host_key_mismatch`/`ssh_host_key_unreachable` as
appropriate. Nothing else in nctl accepts an arbitrary inventory at all.

`nctl reconcile --yes` applies the equivalent binding to its own production-regeneration step: the
post-regeneration SSH scan uses only a `ResolvedSshTarget` from the exact generation just composed
and written, never a snapshot fetched earlier in the same round, and a production route that cannot
be resolved for a target fails closed
(`no_resolvable_production_route`) rather than falling back to mDNS -- mDNS selection is reserved
for the bootstrap phase, which is the only phase guaranteed to still use it.

The supported nctl inventory contract rejects policy-changing SSH variables (including
`ansible_ssh_args`, extra-argument variables, custom SSH executables, non-SSH connections, and
host-key-checking overrides) and accepts only an integer port in `1..65535`. Direct Ansible commands
with hostile CLI options or environment variables are outside that supported path.
