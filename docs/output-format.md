# JSON output envelope

> These envelopes are consumed by the CLI's `--json` output and read by human/agent callers
> shelling out to the CLI, not by any external HTTP subscriber. The wrapper shape and each
> command's `data` field set below are current-consumer contracts under
> [compatibility.md](compatibility.md). A coordinated change updates the writer, readers,
> documentation, and exact contract together.

Every command's `--json` output is a single JSON document on stdout, matching this shape
(`nctl_core.output.Envelope`):

```json
{
  "schema": "nctl.status.v1",
  "generated_at": "2026-07-14T12:00:00+00:00",
  "ok": true,
  "data": { },
  "errors": [
    {"code": "nautobot_unreachable", "message": "cannot reach http://localhost:8000: ...", "detail": {}}
  ]
}
```

## Fields

- `schema` — a current contract identifier such as `nctl.<command>.v1`. A coordinated breaking
  change may update that identifier and every matched consumer; never reuse an identifier for an
  incompatible shape, and do not retain an obsolete runtime writer solely for compatibility.
- `generated_at` — ISO 8601 UTC timestamp of when the envelope was built.
- `ok` — the machine verdict for the whole command. `True` iff `errors` is empty. The process
  exit code mirrors it: `0` when `ok`, `1` when a command ran but `ok` is `false`, `2` for a
  usage/config error that happens before an envelope can even be built (e.g. `nctl.toml` not
  found — those never produce a JSON envelope at all, they print a plain error to stderr).
- `data` — command-specific payload, always present (even a partial/degraded one) regardless of
  `ok`. Each independent check populates as much of `data` as it can and reports its own failure
  as an `errors` entry rather than aborting the whole command.
- `errors` — zero or more `{code, message, detail}` entries. `code` is a short stable identifier
  (e.g. `nautobot_unreachable`, `dump_parse_error`, `submodule_check_failed`) suitable for
  programmatic matching; `message` is human-readable; `detail` is free-form extra context.

## Rendering

Human-readable (non-`--json`) output is always a rendering of the same `data` — each command
implements `render_text(envelope) -> str` and nothing computes text from separate state. Both
modes go through `nctl_core.output.emit(envelope, json_mode, render_text)`. `--json` prints the
envelope only; all diagnostics go to stderr, never stdout, so `--json` output is always valid
JSON when parsed on its own.

## Example: `nctl.status.v1`

See [`status.py`](../src/nctl_core/status.py) `StatusData` for the authoritative shape. Summary:

```json
{
  "operation_id": "01J...",
  "nautobot": {"reachable": true, "url": "http://localhost:8000", "version": "3.1.3", "authenticated": true, "intent_catalog": true},
  "dumps": {"dir": "/var/lib/nodeutils", "hosts": [{"hostname": "agpc", "collected_at": "...", "age_hours": 12.5}], "errors": []},
  "submodules": [{"name": "nintent", "commit": "...", "state": "clean"}]
}
```

`nautobot`, `dumps`, and `submodules` each degrade independently: e.g. an unreachable Nautobot
still yields real `dumps`/`submodules` data, with `ok: false` and a `nautobot_unreachable` entry
in `errors`.

## `nctl.apply.dnsmasq.v2`

The apply envelope cross-references `data.operation_id`, `data.event_log_path`, and the staged
`data.artifact_path`. It also records the resolved inventory, selected target hosts, render
summary, execution mode (`dry-run` or `apply`), SSH preflight entries, daemon `setup` result, and
the records-deploy Ansible command's exit code, stdout, stderr, and parsed per-host recap. A
dry-run that reports changed hosts is still successful when Ansible exits zero; the diff is the
deliverable.

## `nctl.render.dnsmasq.v3`

The renderer returns deterministic records, reservations, ranges, `conf`, and `content_sha256`
for the current desired DNS data. `partial_conf_preview`, `blocked`, and `blocking_findings`
describe a fail-closed render: blocked output is not authoritative deployable content. Reconcile
and Ansible consume the successful render contract.

## `nctl.drift.v1`

```json
{
  "generated_at": "2026-07-16T11:10:48.075090+00:00",
  "summary": {"converged": 3, "unknown": 2},
  "severity_summary": {"error": 2, "warning": 9, "info": 0},
  "targets": [
    {
      "target": {"kind": "node", "slug": "agdnsmasq", "name": "agdnsmasq", "id": "27818c12-..."},
      "status": "unknown",
      "diffs": [
        {
          "target": {"kind": "node", "slug": "agdnsmasq", "name": "agdnsmasq", "id": "27818c12-..."},
          "code": "missing_actual_node",
          "severity": "error",
          "message": "agdnsmasq: missing_actual_node",
          "desired": {},
          "actual": {},
          "sources": ["desired", "actual"]
        }
      ]
    }
  ],
  "sources": {"fetched_at": "2026-07-16T11:10:48.315936+00:00", "observed_dump_count": 1, "observed_errors": []}
}
```

`summary` counts targets by `status` (`converged`/`drifting`/`converging`/`unknown`, derived per
target from its diffs, never persisted); `severity_summary` counts diffs across all targets by
`severity` (`error`/`warning`/`info`). `targets` is sorted by `(target.kind, target identity,
diff code)` regardless of comparator registration order (see `src/nctl_core/drift/registry.py`).
A target with zero diffs is still present (as `converged`) — every desired node/service is seeded
up front so silence in the payload always means "nothing wrong", never "we forgot to check".
`target.kind` is an open string set, not a closed enum: most targets are `"node"`/`"service"`,
but a comparator may also emit a `"device"`-scoped or `"global"`-scoped diagnostic that isn't
owned by one desired node or service (e.g. a production-composition contract error). Additions to
this shape are cheap; renames are expensive — treat it as the stable Phase 3/4 interface it is
(Phase 4's reconcile input).

## `nctl.lifecycle.v1`

```json
{
  "schema": "nctl.lifecycle.v1",
  "generated_at": "2026-07-21T00:00:00+00:00",
  "ok": true,
  "data": {
    "node_id": "27818c12-fe15-4c9f-83d0-7949523f6c33",
    "node_slug": "agpc",
    "previous_state": "planned",
    "requested_state": "active",
    "current_state": "active",
    "changed": true
  },
  "errors": []
}
```

`data` always reflects a confirmed state: `current_state` is read back through GraphQL after the
PATCH, never assumed from the request. `changed: false` means the node was already in
`requested_state` and no PATCH was sent (`previous_state == current_state ==
requested_state`). Errors are command-scoped (`invalid_lifecycle`, `unknown_node`,
`lifecycle_update_rejected`, `lifecycle_confirmation_mismatch`) and never enter
`drift.registry` or `reconcile.classify.CODE_CLASSIFICATION`.

## `nctl.braindump.*.v1`

Five schemas, one per `nctl braindump` command (see the README's `braindump` section for command
semantics). All share the same nested shapes:

```text
AlignmentReviewRecord: id, summary, created, last_updated
BrainDumpRecord: id, title, body, authorship, created, last_updated,
                 review_present, attention, alignment_review (nullable)
BrainDumpListItem: id, title, authorship, created, last_updated,
                   review_present, review_id, review_last_updated, attention
                   (omits body/summary -- list is a compact projection)
```

`authorship` is `user_direct` or `agent_transcribed`; `attention` is `unreviewed`,
`needs_attention`, or `review_present` (see the README for exact meaning — it is a timestamp-only
hint, never a correctness/alignment verdict).

| Schema | `data` fields | Notes |
|---|---|---|
| `nctl.braindump.list.v1` | `items: BrainDumpListItem[]`, `count` | GraphQL read only |
| `nctl.braindump.show.v1` | `braindump: BrainDumpRecord \| null` | full record, including opaque `body`/`summary` |
| `nctl.braindump.create.v1` | `braindump`, `changed` | `changed` is always `true` — create never guesses an existing identity |
| `nctl.braindump.review.v1` | `braindump`, `action` (`"created"` \| `"replaced"`) | always performs a write, even for an identical summary, to advance `last_updated` |
| `nctl.braindump.review_delete.v1` | `braindump`, `deleted`, `review_id` | absent review is a successful no-op: `deleted: false`, `review_id: null` |

Example, `nctl.braindump.review.v1`:

```json
{
  "schema": "nctl.braindump.review.v1",
  "generated_at": "2026-07-21T00:00:00+00:00",
  "ok": true,
  "data": {
    "braindump": {
      "id": "11111111-1111-1111-1111-111111111111",
      "title": "Home lab",
      "body": "Keep Ollama on agpc.",
      "authorship": "user_direct",
      "created": "2026-07-20T00:00:00+00:00",
      "last_updated": "2026-07-20T00:00:00+00:00",
      "review_present": true,
      "attention": "review_present",
      "alignment_review": {
        "id": "22222222-2222-2222-2222-222222222222",
        "summary": "agpc already runs Ollama; no drift.",
        "created": "2026-07-21T00:00:00+00:00",
        "last_updated": "2026-07-21T00:00:00+00:00"
      }
    },
    "action": "created"
  },
  "errors": []
}
```

Every supported write/delete is confirmed via a fresh GraphQL refetch before its envelope reports success; a
mismatch never fabricates `ok: true` and instead returns a `*_confirmation_mismatch` error. Errors
are command-scoped: local-input codes (`invalid_braindump_id`, `invalid_authorship`, `invalid_text`,
`input_conflict`, `input_file_error`, `input_file_invalid_utf8`) and
`braindump_not_found` exit 2 (usage); REST validation/write, transport, race-recovery, and
confirmation-mismatch codes exit 1 (failure) with no success claim. None of these codes, or the
Braindump/Alignment Review data itself, enters `drift.registry`, `reconcile.classify`, or
event-log actuation semantics.

(`review_conflict` is reserved in the design plan for a review uniqueness conflict but is never
emitted by the current implementation: `nctl braindump review`'s bounded race recovery resolves
every uniqueness race internally, surfacing only `review_write_rejected`/
`review_confirmation_mismatch` if that recovery itself fails.)
