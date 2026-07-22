# nctl

Unified CLI for [pj-clusterintent](https://github.com/iwaag/pj-clusterintent): computes desired/actual
drift and runs standard workflows. Implementation plan: `devdocs/vision/core_reconcile/` in the parent repo.

## Layout

- `src/nctl_core/` — the library. All business logic lives here and returns pydantic models.
- `src/nctl_core/cli/` — thin Typer wrappers. Commands parse args, call the library, render.

## Setup

```bash
uv sync
cp example.nctl.toml ../nctl.toml   # at the pj-clusterintent root, git-ignored
export NAUTOBOT_TOKEN=...           # or set nautobot.token_file
```

## Recipes

- [`register a new PC`](docs/register-a-new-pc.md) — new machine to converged, intent-only.
- [`add a basic service`](docs/add-a-basic-service.md) — place a service on an existing node.

## Usage

```bash
uv run nctl status
uv run nctl status --json
uv run nctl render dnsmasq
uv run nctl render hosts-intent
uv run nctl render hosts-intent --out ../ansible_agdev/inventories/generated
uv run nctl render production
uv run nctl render production --out ../ansible_agdev/inventories/generated
uv run nctl drift
uv run nctl drift --host agstudio --json
uv run nctl apply dnsmasq
uv run nctl apply dnsmasq --yes
uv run nctl dashboard
uv run nctl dashboard --no-push
uv run nctl dashboard --from ~/.local/state/nctl/dashboard/drift.json --out /tmp/preview
uv run nctl reconcile
uv run nctl reconcile agstudio
uv run nctl reconcile agstudio --yes
uv run nctl reconcile --yes --max-rounds 1 --json
uv run nctl ops list
uv run nctl ops list --limit 5 --json
uv run nctl ops show 01KXPYQRJ8GTNND0PC3KZSMPXC
uv run nctl ops show 01KXPYQRJ8GTNND0PC3KZSMPXC --after-seq 3 --json
uv run nctl braindump list
uv run nctl braindump show <braindump-id>
uv run nctl braindump create --title "Home lab" --authorship user_direct --body "Keep Ollama on agpc."
uv run nctl braindump create --title "Home lab" --authorship user_direct --file wish.txt
uv run nctl braindump update <braindump-id> --title "Home lab v2"
uv run nctl braindump review <braindump-id> --summary "agpc already runs Ollama; no drift."
uv run nctl braindump review-delete <braindump-id> --yes
uv run nctl braindump delete <braindump-id> --yes
uv run --extra serve nctl serve
uv run --extra serve nctl serve --host 0.0.0.0 --port 8300
```

`status` checks Nautobot connectivity/auth/intent-catalog presence, nodeutils dump freshness, and
parent-repo submodule state. Each of the three checks degrades independently: e.g. an unreachable
Nautobot still yields dump and submodule info, with `ok: false` and an entry in `errors`.

`render dnsmasq` fetches desired endpoints, IP ranges, and actual node/interface state through GraphQL and
prints a deterministic dnsmasq configuration. Use `--out PATH` to write the configuration or
`--json` to inspect the complete render payload.

`render hosts-intent` fetches desired nodes through GraphQL and emits the minimal mDNS bootstrap
inventory used before actual facts are collected. Without `--out`, YAML goes to stdout. With
`--out DIR`, nctl validates a staged copy using `ansible-inventory --list`, atomically replaces
`DIR/hosts_intent.yml`, and writes `DIR/hosts-intent-export.json`. The JSON envelope schema is
`nctl.render.hosts_intent.v1`. The command name is deliberately `hosts-intent`, rather than the
ambiguous `inventory`, because `render production` creates the canonical operational inventory.

`render production` reads `ansible_agdev/vars/deployment_profiles.yml` directly, joins desired
placements and operational policy with Nautobot actual facts, and emits the schema 1.0 production
inventory. Without `--out`, YAML goes to stdout. With `--out DIR`, nctl validates a staged copy
using `ansible-inventory --list`, writes `DIR/production.reports/<generation_id>.json`, and
atomically replaces `DIR/production.yml`. The JSON envelope schema is
`nctl.render.production.v1`; `data` contains `inventory`, `report`, `inventory_yaml`, and
`report_json`.

`drift` computes the current three-source reconciliation result synchronously. Desired state comes
from nintent through GraphQL, actual ledger state comes from Nautobot, and observed state comes from
nodeutils dumps. `--host SLUG` and `--service NAME` filter targets. Finding drift is a successful
answer (exit 0); only a failed run such as authentication or fetch failure returns exit 1.

The `nctl.drift.v1` envelope contains:

- `summary` and `severity_summary` counts;
- `targets`, each with `target`, derived `status`, and sorted structured `diffs`;
- `sources` with fetch time, observed dump count, and dump errors;
- `generated_at`.

Target statuses are `unknown` when required actual data is missing/stale, `drifting` when an
error-severity diff exists, `converging` when a newer targeting operation exists than the latest
observation, and `converged` when only warning/info diffs or no diffs remain. Each diff provides a
stable `code`, `severity`, small `desired`/`actual` evidence values, contributing `sources`, and a
human-readable `message`.

`apply dnsmasq` renders into an operation-specific artifact, then runs the daemon-install playbook
(`playbooks/bootstrap/setup_dnsmasq.yml`) followed by the deploy-only Ansible playbook, both in
`--check --diff` mode by default (a setup failure aborts before the records deploy runs). Review
that output, then use `--yes` for the real apply. The configured inventory must resolve at least
one host in `dnsmasq_server`; an existing inventory file with an empty or missing group is rejected
instead of succeeding as a no-op.

`apply dnsmasq --inventory PATH` overrides the configured `[ansible].inventory` for that one run —
the bootstrap escape hatch for a freshly registered node that has no production inventory entry
yet. No silent fallback: omit `--inventory` and it uses the configured production inventory as
always; `reconcile` never passes an override, it always actuates against the production inventory
it regenerates itself. Bootstrap sequence for a brand-new dnsmasq node (see
[`add a basic service`](docs/add-a-basic-service.md) for declaring the placement first):

```bash
uv run nctl render hosts-intent --out ansible_agdev/inventories/generated
uv run nctl apply dnsmasq --inventory ansible_agdev/inventories/generated/hosts_intent.yml
uv run nctl apply dnsmasq --inventory ansible_agdev/inventories/generated/hosts_intent.yml --yes
```

Once nodeutils collection + ingest have run against the new host, `nctl render production` and
subsequent `nctl apply dnsmasq`/`nctl reconcile` runs use the regenerated production inventory as
usual — the override is only for the one-time bootstrap window before it exists.

`dashboard` is the routine command for getting the drift picture in front of a human: it runs a
fresh `nctl drift` internally, renders a single self-contained `index.html` (color-coded tiles,
one per target, that expand on click into their diffs) alongside the exact `drift.json` payload
that produced it, and — unless `--no-push` is given — writes each target's status back into
nintent (see [Status legend](#status-legend) and "Status write-back" below). **Run `nctl
dashboard`, not `nctl drift`, whenever you want the page updated** — `drift` itself never writes
anything, by design. `--out DIR` overrides `[dashboard].out_dir`. `--from FILE` skips the live
drift computation and re-renders (and, unless `--no-push`, re-pushes) a previously saved
`nctl.drift.v1` envelope — useful for offline preview or replaying a saved payload. A failed
drift run still produces a page: the envelope's `ok: false` and its errors are embedded and
rendered visibly, so a broken run doesn't silently leave a stale-looking dashboard with no
indication anything went wrong. Status-push failures never fail the command or block the file
write — they degrade into the `status_push` counts in the returned `nctl.dashboard.v1` envelope
(`attempted`/`updated`/`skipped_no_row`/`failed`, with a message per failed target).

### Status legend

Dashboard tiles and nintent's reconciliation-status badges use one color mapping:

| status | color | meaning |
|---|---|---|
| `converged` | green | no error-severity diffs |
| `converging` | yellow | diffs exist, but a newer `apply`/`reconcile` operation targets this node than its latest actual observation — change is in flight |
| `drifting` | red | an error-severity diff exists and nothing in flight explains it |
| `unknown` | gray | required actual data is missing, stale, or never linked — nctl cannot see this target, which is different from it having drifted |

### Status write-back

After a successful drift run and file write, `dashboard` PATCHes `reconciliation_status` +
`reconciliation_checked_at` onto each target's ledger row in nintent (`DesiredNode` for
`kind: "node"` targets, `DesiredService` for `kind: "service"`, matched by `target.id`) through
the intent-catalog REST ViewSets — reads go through GraphQL project-wide, but this is a write, so
it goes through REST per the project's read/write split. Nautobot being unreachable, a target
having no ledger row (`skipped_no_row`), or any other PATCH failure only ever produces a warning
entry in `status_push`; it never flips the command's `ok` or blocks the HTML/JSON write. These
fields are a **derived cache of the last nctl run**, not a second source of truth — `nctl drift`
remains authoritative; `reconciliation_checked_at` is what makes a stale cache visible in
nintent's UI.

### `reconcile`

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
  **before any mutation** (`manual_intervention_required`); a controller-local lock held by another
  reconcile fails immediately (`reconcile_lock_contention`) before the first drift fetch. Exit 0
  only for `already_converged`/`converged`; every other apply-mode state exits 1.
- **Scope**: an independent target's failure never blocks other independent targets in the same
  scope — the run still reports the overall result as non-`converged` if any selected target never
  reaches a fresh `converged` status, but reachable/healthy targets still make progress. A host
  argument must resolve to exactly one desired-node slug; zero or multiple matches are a usage
  error (exit 2), not a run failure.
- **Dashboard reuse**: every apply terminal path that has a valid full-cluster drift payload
  refreshes the same dashboard/status cache from that exact payload — `build_drift` is never called
  a second time, so the dashboard and the reconcile result can't disagree. A dashboard write-back
  failure degrades to a warning in `data.dashboard` and never changes the reconcile terminal
  `state`.
- **Audit trail**: before `--yes` mutates anything, nctl verifies the operation directory and event
  log are writable and refuses to proceed if they aren't (`artifact_write_failed`) — a mutating run
  never proceeds without a place to record what it did.

The `nctl.reconcile.v1` envelope's `data` carries `operation_id`, `mode`, `scope`, terminal `state`
(`planned | already_converged | converged | manual_intervention_required | non_converged | failed`),
`event_log_path`, `artifact_dir`, `plan_path`, initial/final drift paths, per-round action results
(`rounds`), `manual_review`/`unsupported` records (target + diff code + evidence), scope/global
status summaries, `ssh_preflight` (below), and the `dashboard` result. The plan itself
(`<events.log_dir>/<operation_id>/plan.json`, schema `nctl.reconcile.plan.v1`) is both embedded in
plan-mode output and persisted standalone; it never contains a Nautobot token, raw report content,
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
```

**SSH trust preflight** (`devdocs/small/fix_sshkey/plan.md` Step 5): every round, before observation,
Nautobot Jobs, inventory writes, or playbooks run, nctl checks that every node touched by an
SSH-requiring action (`observe_node`, `service_profile`, `dnsmasq_config`) has at least one entry
under its stable alias in `[ssh].known_hosts_file`. A missing entry fails the whole round with
`ssh_host_key_unenrolled` and the exact `nctl ssh enroll <slug>` remediation, before any write --
this is what prevented the original incident (`agdnsmasq`'s observation/IPAM succeeding, then
failing on the production dnsmasq SSH connection). Ledger-only actions (`link_actual_node`,
`reconcile_ipam`) never require enrollment, so an unrelated unenrolled host never blocks them.
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

### `lifecycle`

`nctl lifecycle NODE STATE [--json]` is a direct, idempotent setter for one `DesiredNode`'s
`lifecycle` (`planned`, `approved`, `active`, `deprecated`, `retired`) — it is **not** an approval
engine and is not part of `reconcile --yes`; nothing in `reconcile` changes lifecycle automatically.
Nodes are created `active` by default (Better Usability Phase 3), so this command exists for
deliberate staging/promotion/demotion, not routine registration.

`NODE` must be an exact desired-node slug. The command resolves it through the same GraphQL read
path as every other command, no-ops (`changed: false`, no write) if the node is already in the
requested state, otherwise PATCHes only `{"lifecycle": STATE}` to the intent-catalog `nodes`
ViewSet, then refetches through GraphQL and fails closed (`lifecycle_confirmation_mismatch`) unless
the write is confirmed. Text output is `NODE: before -> after` or `NODE: already STATE (no
change)`; `--json` prints the closed `nctl.lifecycle.v1` envelope (`node_id`, `node_slug`,
`previous_state`, `requested_state`, `current_state`, `changed`). `unknown_node` and
`invalid_lifecycle` are usage exits (2); a rejected PATCH or confirmation mismatch is a failure exit
(1) with no success claim. No new drift/reconcile classification code is introduced — promoting a
node only makes it eligible for whatever findings already applied to `active`/`approved` nodes.

### `ops list` / `ops show`

`nctl ops list [--limit N] [--json]` and `nctl ops show OPERATION_ID [--after-seq N] [--json]` are
a read-only, filesystem-only view over `[events].log_dir` — no live process, Nautobot, or Ansible
access required, so they work equally well against operations started by the CLI or by `nctl
serve`. `ops list` enumerates every `<operation_id>.jsonl` file, newest first, parsing just enough
of each to report `op`/`state`/`ok`/`result`/timestamps (schema `nctl.ops.list.v1`). `ops show`
additionally returns the full event list (or only events with `seq > --after-seq`, the same cursor
convention as the WebSocket replay below) plus the resolved `artifact_dir` and its artifact list,
using the same corrupt-line-tolerant JSONL reader as the server (schema `nctl.ops.show.v1`; a
truncated or partially written final line is reported via `corrupt_lines`, not raised as an error).
This module (`nctl_core.operations_index`) is what both the CLI and `nctl serve`'s
`/api/v1/operations*` endpoints are built on, so `nctl ops show` and the equivalent HTTP call
return the same data.

### `braindump`

`nctl braindump {list,show,create,update,delete,review,review-delete}` is the deterministic,
typed interface to the exchange diary described in `devdocs/big/braindump/roadmap.md`: a
**Braindump** is the user's free-form wish, and its at-most-one current **Alignment Review** is the
AI agent's latest natural-language reply. Neither is executable input, and this command surface has
no import path into `drift`, `reconcile`, dashboard, `serve`, Jobs, nodeutils, or Ansible — reading
or writing the diary never changes convergence status or triggers actuation.

- `list [--json]` / `show ID [--json]` read through GraphQL only and never write. `list` returns a
  compact `id`/`title`/`authorship`/timestamps/review-presence/attention projection; `show` returns
  the full record including `body` and, if present, the review's `summary`.
- `create --title TITLE --authorship AUTHOR (--body TEXT | --file PATH)` and
  `update ID [--title TITLE] [--authorship AUTHOR] [--body TEXT | --file PATH]` write through REST
  and always confirm the result via a fresh GraphQL refetch before reporting success; a mismatch is
  a command-scoped `*_confirmation_mismatch` failure, never a fabricated success. `AUTHOR` is
  exactly `user_direct` or `agent_transcribed` — there is no default, so provenance is never
  misstated. `update` preserves every omitted field and requires at least one supplied change.
- `--file PATH` reads the file as `Path.read_text(encoding="utf-8", errors="strict")` — the exact
  bytes are stored, with no trailing-newline stripping, line-ending normalization, BOM removal,
  Markdown rendering, variable interpolation, or shell/prompt interpretation. Prefer `--file` over
  `--body` for multiline or shell-sensitive prose, and never embed secrets in either — command-line
  arguments and stored Braindump text both end up in process lists, reports, and Git history.
- `review ID (--summary TEXT | --file PATH)` creates the review when none exists and replaces the
  one current row when it does — it never appends a second row. Replacement always advances
  `last_updated`, even when the new summary text is byte-identical to the old one, because invoking
  `review` records a new evaluation. A rare create/create race (two writers, no existing review) is
  recovered automatically by refetching once and replacing the row the other writer created; any
  other rejection is a genuine validation failure and is reported as such.
- `delete ID [--yes]` deletes a Braindump and, by database cascade, its current review with it.
  `review-delete ID [--yes]` deletes only the review, returning the Braindump to the unreviewed
  state; deleting an already-unreviewed Braindump's review is an idempotent no-op
  (`deleted: false`), not an error. Both destructive commands prompt for the exact target UUID
  without `--yes` in human mode; `--json` is non-interactive and requires `--yes` or fails as a
  usage error (exit 2) before contacting Nautobot. `--yes` never broadens the target — there is no
  bulk, title-based, or wildcard delete.
- Attention is a non-persisted, three-state hint computed only from the two diary timestamps:
  `unreviewed` (no review row), `needs_attention` (the review is older than its Braindump), or
  `review_present` (a review exists and is not older than its Braindump). `review_present` does
  **not** mean aligned, valid, or converged — it says only that a current review row exists.
  Braindump/review timestamps are never compared against desired/actual freshness here; run `nctl
  drift --json` separately and read its evidence before writing a grounded review.

A safe external-agent interaction over this surface: read `braindump list --json` and the relevant
`show --json` calls, read `nctl drift --json` separately for desired/actual evidence, ask the user
about ambiguity or any proposed structured change, write only the user's confirmed words to a
Braindump, publish the agent's own prose with `braindump review`, and use the established
desired-state/`reconcile` commands separately — only after the user has actually granted that
authority, never inferred from Braindump/review text alone.

Each command emits its own frozen `nctl.braindump.<command>.v1` envelope (see
[`docs/output-format.md`](docs/output-format.md)); human output renders **User-originated
Braindump** and **AI Alignment Review** as visually separate sections so AI-derived text is never
mistaken for the user's own words.

## Serve (realtime API)

`nctl serve [--host] [--port] [--json]` wraps the same `nctl_core` functions the CLI calls behind
an HTTP + WebSocket API (FastAPI/uvicorn), so an external process — a game-engine UI, a voice
frontend, a script — can read state and trigger operations without shelling out to the CLI. It is
an optional extra: `uv sync --extra serve` (or `uv run --extra serve nctl serve`) pulls in
`fastapi`/`uvicorn`; a plain `uv sync` install has no ASGI dependencies and cannot run `serve`.
Default bind is `127.0.0.1:8300`; printing the `nctl.serve.v1` startup envelope and then running
uvicorn in the foreground until `Ctrl-C`.

### Config and auth

```toml
[serve]
host = "127.0.0.1"
port = 8300
token_env = "NCTL_SERVE_TOKEN"
# token_file = "~/.config/nctl/serve_token"
auth = "token"          # or "none"
cors_origins = []        # e.g. ["http://192.168.1.50"] for a browser UI on another LAN host
```

Following the exact `[nautobot]` convention, the token is never accepted inline in `nctl.toml`
(`extra="forbid"` plus no `token` field at all) — set `NCTL_SERVE_TOKEN` or `token_file`. Startup
fails fast (before uvicorn binds) if `auth = "token"` and no token resolves from either source, so
there is no accidental "auth off because nothing was configured" state. `auth = "none"` is an
explicit opt-out for loopback-only experiments and is rejected by config validation if `host` is
not a loopback address. Every HTTP request other than `GET /api/v1/health` and `GET /` requires
`Authorization: Bearer <token>`; token comparison uses `secrets.compare_digest`. The WebSocket
handshake accepts the same header, with a `?token=` query-string fallback only for clients that
cannot set headers. The token never appears in logs, envelopes, events, or the OpenAPI document.

### Endpoints (`/api/v1`)

| Method & path | Meaning |
|---|---|
| `GET /api/v1/health` | liveness + version; unauthenticated |
| `GET /api/v1/status?refresh=false` | last persisted `nctl.status.v1` snapshot; `refresh=true` computes a fresh one inline (the one synchronous exception — cheap enough to not need an operation) |
| `GET /api/v1/drift` | latest persisted `nctl.drift.v1` payload, from whichever drift-producing operation (`drift`/`dashboard`/`reconcile`) wrote it most recently |
| `GET /api/v1/operations?limit=N` | recent operations, newest first |
| `GET /api/v1/operations/{id}` | one operation's record plus its terminal `result.json` if finished |
| `GET /api/v1/operations/{id}/events?after_seq=N` | events after cursor `N` (`-1` for all), read straight from the JSONL file |
| `GET /api/v1/operations/{id}/artifacts` / `.../artifacts/{name}` | list/fetch allowlisted artifacts (`plan.json`, drift rounds, `result.json`); anything mode `0600` (reports, probe configs, job payloads) is never served |
| `POST /api/v1/operations` | body `{"op": "drift" \| "dashboard" \| "render.dnsmasq" \| "render.production" \| "render.hosts_intent" \| "reconcile", "params": {...}}` (params mirror the equivalent CLI flags) → `202 {operation_id, op, mutating, events_url, ws_url}` |
| `WS /api/v1/ws` | event stream (protocol below) |
| `GET /` | the reference live dashboard page (Decision 8; unauthenticated — the token is entered client-side and only ever sent to `/api/v1/*`) |
| `GET /openapi.json` | generated OpenAPI document (authenticated) |

Errors use HTTP status plus the same `EnvelopeError` shape (`{code, message, detail}`) every CLI
command already returns: `401` unauthorized, `404` unknown ID/artifact, `409` single-flight
conflict (`detail` names the running `operation_id`), `422` validation, `503` no persisted
snapshot yet. The terminal envelope reachable via `GET /api/v1/operations/{id}` is byte-identical
(modulo `operation_id`/timestamps) to what the CLI's `--json` prints for the same run, and the
JSONL/artifact layout on disk is identical regardless of which path triggered the operation.

### Single-flight execution

Every `POST /api/v1/operations` runs on a worker thread, never on the request/event-loop thread —
`nctl_core` is synchronous throughout and an applying reconcile can run for minutes. The server
keeps one in-process gate: any mutating operation (`reconcile` with `yes=true`; `dashboard`, which
always pushes statuses; `render.production`/`render.hosts_intent` with `write=true`) excludes every
other mutating operation, and a concurrent mutating `POST` gets `409` with the running operation's
ID instead of queueing. Non-mutating operations (`drift`, plan-mode `reconcile`, renders without
`write`) can run concurrently with each other but are still serialized against a running mutating
operation. Underneath, the executor still acquires the Phase 4 controller-local file lock
(`[reconcile].lock_path`), so a server-triggered apply and a human running `nctl reconcile --yes`
in a terminal exclude each other in both directions, not just server-side.

### WebSocket protocol and replay

Connect to `ws://HOST:PORT/api/v1/ws`, authenticate via header or `?token=`, then send one JSON
subscribe message:

```json
{"subscribe": "all", "after_seq": -1}
{"subscribe": {"operation_id": "01K..."}, "after_seq": 3}
```

`after_seq` is the last `seq` the client already has for that operation (`-1` for everything). The
server first replays every newer record from the operation's JSONL file, then attaches to the
in-process event bus for live records, de-duplicating by (`operation_id`, `seq`) across the
replay/live boundary — because `seq` is monotonic per operation and the file is authoritative, a
client can disconnect at any point, reconnect with the last `seq` it saw, and provably miss
nothing. Frames are exactly the `EventRecord` JSON already written to the JSONL file — no second
wire schema. A client that falls behind (bounded per-connection queue) is disconnected with close
code `4408` and is expected to reconnect and replay via `after_seq` rather than being buffered
unboundedly; a bad/missing subscribe message within 30s closes with `4400`. Failed auth is rejected
before the handshake ever upgrades (`websocket.close()` is called ahead of `accept()`), so a real
client sees the connection refused at the HTTP layer (`403`) rather than a WS close frame — the
`4401` code is what an ASGI-level test harness (no real socket) observes for the same rejection;
either way, no data is ever sent to an unauthenticated caller.

### Reference live dashboard

`GET /` serves one build-toolchain-free HTML page in the same visual language as the static Phase 3
dashboard: it fetches `/api/v1/drift` and `/api/v1/operations` on load, then subscribes over the
WebSocket for live tile updates, plus an operations sidebar with recent/running operations and
their event tail. It offers exactly two actions — refresh drift, plan-only reconcile — both just
`POST /api/v1/operations`; applying reconcile stays CLI-only in this phase. The token is pasted
once into the page and kept in `sessionStorage`, never embedded in the served HTML. This page uses
only the documented API above, so it doubles as the proof that a future game-engine or voice UI can
be built on top without any backend changes. It is a validation instrument, not a replacement for
`nctl dashboard`: the static artifact and its file/LAN hosting are untouched.

Compatibility posture for all of the above (event shape, event vocabulary, envelope fields, the
`/api/v1` surface) is frozen additive-only from this phase on — see
[`docs/compatibility.md`](docs/compatibility.md).

## Ansible configuration

```toml
[ansible]
playbook_dir = "ansible_agdev"
inventory = "inventories/generated/production.yml"
```

`playbook_dir` is the `ansible_agdev` checkout, resolved relative to `nctl.toml` when not absolute.
A relative `inventory` path resolves inside that checkout; an absolute inventory file or directory
is also accepted. Both `ansible-inventory` and `ansible-playbook` must be on `PATH`.
The bootstrap `hosts_intent.yml` does not contain service groups and therefore cannot select
`dnsmasq_server`; generate the current production inventory with `nctl render production --out`.

Each apply stores its rendered conf at
`<events.log_dir>/<operation_id>/artifacts/dnsmasq-records.conf` and its JSON Lines event log at
`<events.log_dir>/<operation_id>.jsonl`.

## Dashboard configuration

```toml
[dashboard]
out_dir = "~/.local/state/nctl/dashboard"   # default; where index.html + drift.json land
url = "http://192.168.1.50/nctl-dashboard/" # optional: where out_dir is served on the LAN
```

`url` is purely informational — nctl never fetches it. It is surfaced in the
`nctl.dashboard.v1` envelope's `dashboard_url` field and is the value to also set in nintent's
`PLUGINS_CONFIG["nautobot_intent_catalog"]["dashboard_url"]` so the plugin's nav link and
per-object "(view dashboard)" links point at the same place. The two settings are independent —
nctl does not read nintent's plugin config, and nintent does not read `nctl.toml` — keep them in
sync by hand when `out_dir`'s serving location changes.

## SSH trust configuration

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

### Lifecycle

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

### Hardware replacement and key rotation

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

### Recovering from a lost or corrupted managed file

If `[ssh].known_hosts_file` is lost, corrupted, or reset, the only supported recovery is
re-enrolling each node (`nctl ssh enroll <slug> --from-known-hosts` or `--fingerprint ...`, per
node, through the same verified-source rules above). Do not "fix" a missing/broken managed file by
setting `StrictHostKeyChecking=no`, using `accept-new`, or copying in an unverified `ssh-keyscan`
result -- none of those are a substitute for a verified source, and all of them defeat the fail-closed
guarantee this store exists to provide.

### Direct Ansible use

Both generated inventories (`hosts_intent.yml` and `production.yml`) carry the same closed, strict
host variables (`nctl_ssh_host_key_alias`, `ansible_ssh_common_args` with
`StrictHostKeyChecking=yes`), so a direct `ansible`/`ansible-playbook` invocation against either one
fails closed exactly like `nctl` does, just with OpenSSH's generic `Host key verification failed`
instead of a structured nctl error code. A hand-written or otherwise-sourced inventory that lacks
these variables is outside the supported operational path: `nctl apply dnsmasq` rejects one
(`dnsmasq_inventory_untrusted_host`) rather than silently falling back to endpoint-keyed
verification -- for the normally configured inventory exactly as much as an explicit `--inventory`
override, and in dry-run exactly as much as `--yes` (fix_sshkey2 Step 4; before that fix, only
`--inventory` was checked, and only for alias/node-ID presence). Passing the variable check is not
enough on its own either: `apply dnsmasq` also re-scans the route resolved from the inventory's own
host vars and requires the currently offered key to match a managed entry before Ansible starts,
failing closed with `ssh_host_key_unenrolled`/`ssh_host_key_mismatch`/`ssh_host_key_unreachable` as
appropriate. Nothing else in nctl accepts an arbitrary inventory at all.

`nctl reconcile --yes` applies the equivalent binding to its own production-regeneration step: the
post-regeneration SSH scan always resolves its route from the exact `SourceSnapshot` and generation
that was just composed and written, never a snapshot fetched earlier in the same round, and a
production route that cannot be resolved for a target fails closed
(`no_resolvable_production_route`) rather than falling back to mDNS -- mDNS selection is reserved
for the bootstrap phase, which is the only phase guaranteed to still use it.

## Conventions

- **Config**: `nctl.toml`, resolved as `--config` → `$NCTL_CONFIG` → `./nctl.toml` → parent-repo root.
  Tokens are never stored in the file (rejected by validation); use `token_env` / `token_file`.
- **JSON output**: every command returns a stable `nctl.<command>.v1` envelope via `--json`
  (spec: `docs/output-format.md`).
- **Event logs**: long-running operations emit JSON Lines with an operation ID
  (spec: `docs/event-log.md`).
- **Exit codes**: 0 ok / 1 command failure / 2 usage or config error.
- **Reads vs writes**: reads go through Nautobot GraphQL (`NautobotClient.graphql()`, a single
  unified client for both core DCIM/IPAM and `nintent`'s desired-state types); writes stay REST
  (Nautobot GraphQL is read-only by design, and the intent-catalog ViewSets remain the write path).

## Adding a comparator

Comparators live under `src/nctl_core/drift/` and are registered by resource type:

```python
from nctl_core.drift.registry import register

@register("node")
def compare_example(snapshot, context):
    yield from ()
```

A comparator accepts one `SourceSnapshot` plus `DriftContext` and yields `DiffRecord` values. It
must not depend on registration order: the registry runs resource types deterministically and sorts
the combined output by target identity and diff code. Add focused comparator tests plus an engine
or `nctl.drift.v1` fixture whenever a new code affects target status or consumer behavior.

## Development

```bash
uv run pytest
```
