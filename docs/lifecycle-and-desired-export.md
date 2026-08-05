# `lifecycle` and `desired export`

See [`../README.md`](../README.md#usage) for the full command list.

## `lifecycle`

`nctl lifecycle NODE STATE [--json]` is a direct, idempotent setter for one `DesiredNode`'s
`lifecycle` (`planned`, `approved`, `active`, `deprecated`, `retired`) — it is **not** an approval
engine and is not part of `reconcile --yes`; nothing in `reconcile` changes lifecycle automatically.
Nodes are created `active` by default (Better Usability Phase 3), so this command exists for
deliberate staging/promotion/demotion, not routine registration.

`NODE` must be an exact desired-node slug. The command resolves it through the same GraphQL read
path as every other command, no-ops (`changed: false`, no write) if the node is already in the
requested state, otherwise submits a one-operation desired-state batch that changes only
`{"lifecycle": STATE}`, then refetches through GraphQL and fails closed
(`lifecycle_confirmation_mismatch`) unless the write is confirmed. Text output is `NODE: before -> after` or `NODE: already STATE (no
change)`; `--json` prints the closed `nctl.lifecycle.v1` envelope (`node_id`, `node_slug`,
`previous_state`, `requested_state`, `current_state`, `changed`). `unknown_node` and
`invalid_lifecycle` are usage exits (2); a rejected PATCH or confirmation mismatch is a failure exit
(1) with no success claim. No new drift/reconcile classification code is introduced — promoting a
node only makes it eligible for whatever findings already applied to `active`/`approved` nodes.

## `desired export`

`nctl desired export` reads the complete current desired state (the same pinned GraphQL snapshot
every other desired consumer uses) and emits it as one canonical Phase 0 batch document — the
exact YAML shape `nctl desired apply -f` accepts. There is no second export format: the batch
document is the desired-state file representation, so an export is a re-applyable, human-readable
backup, and the built-in acceptance check is

```bash
uv run nctl desired export > snapshot.yaml
uv run nctl desired apply -f snapshot.yaml --json   # preview must report every operation unchanged
```

Default output is the raw YAML document on stdout; `--json` wraps it in the
`nctl.desired.export.v1` envelope (`document`, per-kind `counts`, `operation_count`). Every
writable field is explicit (apply is a partial upsert, so an omitted field would be silently
"preserved" and mask an incomplete export), operations are stable-sorted by the writer's kind
dependency order then identity, and free-form JSON values are key-sorted, so two exports of
unchanged state are byte-identical and the document also applies onto an empty database. An
unresolved reference, a snapshot field the exporter cannot write back, or a decode-time source
issue that dropped or normalized row data fails the export by name instead of emitting a partial
backup (NIC-readiness exclusions are re-included from the snapshot's `unready_compute_instances`;
they and duplicate-MAC flags leave row data intact and do not block export). Export complements —
does not replace — the PostgreSQL dumps in `.local/backups/`.
