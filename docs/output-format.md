# JSON output envelope

> From Phase 5 onward, external subscribers (not just the CLI) consume these envelopes over
> HTTP. The wrapper shape and each command's `data` field set below are frozen per
> [compatibility.md](compatibility.md) — additions are fine, renames/removals are not.

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

- `schema` — `nctl.<command>.v1`. The suffix is bumped on any breaking change to `data`'s shape;
  this is a pre-freeze project, so bump freely but always explicitly (never reuse a version
  number for an incompatible shape).
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

## `nctl.apply.dnsmasq.v1`

The apply envelope cross-references `data.operation_id`, `data.event_log_path`, and the staged
`data.artifact_path`. It also records the resolved inventory, selected target hosts, render
summary, execution mode (`dry-run` or `apply`), and the Ansible command's exit code, stdout,
stderr, and parsed per-host recap. A dry-run that reports changed hosts is still successful when
Ansible exits zero; the diff is the deliverable.

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
(the dashboard's only input, and Phase 4's reconcile input).

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

## `nctl.dashboard.v1`

```json
{
  "data": {
    "html_path": "/Users/x/.local/state/nctl/dashboard/index.html",
    "drift_json_path": "/Users/x/.local/state/nctl/dashboard/drift.json",
    "generated_at": "2026-07-16T11:10:48.075090+00:00",
    "summary": {"converged": 3, "unknown": 2},
    "severity_summary": {"error": 2, "warning": 9, "info": 0},
    "status_push": {"pushed": true, "attempted": 5, "updated": 5, "skipped_no_row": 0, "failed": 0, "errors": []},
    "dashboard_url": null
  }
}
```

`data.summary`/`data.severity_summary`/`data.generated_at` mirror the drift run that produced the
page (or the `--from` payload's own fields, when given). `status_push` is only attempted for a
successful drift payload with `push` enabled (the CLI default; `--no-push` skips it): `attempted`
counts node/service targets considered, `updated` successful PATCHes, `skipped_no_row` targets
with no matching ledger row, `failed` PATCH failures — each with a `"<kind> <slug-or-id>: <error>"`
string in `errors`. Push failures never affect `ok`; only a drift-run failure or a file-write
failure does (both surface in the top-level `errors` list, and — for a drift-run failure — inside
the rendered page itself, since the page embeds this same envelope's drift data). `dashboard_url`
echoes `[dashboard].url` from `nctl.toml` (`null` if unset) — informational only, never fetched.
