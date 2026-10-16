# `observe`

See [`../README.md`](../README.md#usage) for the full command list.

`nctl observe [HOST]… [--operation-id ULID] [--json]` collects fresh nodeutils
observations and ingests them into Nautobot, and does nothing else. It runs
exactly the `observe_node` pipeline that `reconcile` uses (collection
playbook, report retrieval, identity and age validation, controller cache,
the *Ingest Nodeutils Inventory* Job) under the same reconcile lock, with the
same operation record and artifacts. It never computes drift, plans or
dispatches any other reconciler: a host with repairable drift is observed and
left as it is. Converging is `reconcile`'s job.

**Scope.**

- Every explicit `HOST` must be exactly one desired-node slug that the
  bootstrap inventory exports (active lifecycle, an mDNS endpoint). Anything
  else is a usage error (exit 2) before an operation record exists; an
  unresolved host never widens or narrows the set silently.
- With no host, the scope is every exported node. Nodes the inventory skips
  are listed in `data.skipped` with their reasons.
- Each scoped host's managed SSH key is checked against what its mDNS route
  offers first (the same bootstrap scan `reconcile` runs). A host that is
  unenrolled, unreachable (asleep, off the network) or offers a different key
  is **not contacted** and is reported `unavailable`; the rest are observed.

**Result (`nctl.observe.v1`).** `data.hosts[]` has one row per scoped host:

| outcome | meaning |
| --- | --- |
| `observed` | a fresh report was collected, validated, cached and ingested (`created`/`updated`/`unchanged`) |
| `unavailable` | not contacted; `ssh_status` and `detail` say why |
| `collection_failed` | contacted, but no valid fresh report was retrieved |
| `ingest_failed` | a report was collected but the Job skipped or failed it |

`data.state` is `observed` (every host observed), `partial` (some) or
`failed` (none). Errors carry `host_unavailable`, `host_collection_failed`,
`host_ingest_failed`, `reconcile_lock_contention`, `ssh_store_read_failed` or
`artifact_write_failed`. Exit codes: 0 for `observed`; 1 for `partial` or
`failed`, or when desired state cannot be read; 2 for a usage error. The
terminal envelope is also written to the operation's `result.json`, so
`nctl ops show <id>` reads it later.

**`--operation-id`** (also on `reconcile`) uses a caller-chosen ULID that has
not been used before, so a caller can record the id before the operation
starts and find it again with `nctl ops show` if it never sees the output.

## `reconcile --allowed-hosts`

`nctl reconcile HOST --yes --allowed-hosts a,b` bounds every action of every
round: before dispatch, an action whose contacted hosts (`host_slugs`) — or,
for ledger-only actions, node targets — fall outside the set is refused, the
round stops with `action_outside_allowed_hosts`, and nothing of it runs. In
plan mode the envelope lists such actions in `data.outside_allowed_hosts`.
It complements `--allow-destroy`: a caller that never passes that flag never
reaches a destroy, and a caller that passes `--allowed-hosts` never reaches a
host it did not name, even when a later round or a multi-placement service
profile would.
