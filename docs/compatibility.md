# Compatibility policy (frozen from Phase 5 onward)

Phase 5 gives `nctl_core` external subscribers (HTTP/WebSocket clients, not just the CLI and a
human reading its stdout). From this phase onward the shapes below are **frozen**: they may grow
additively, but existing fields/names never change meaning or disappear within the same version.
Schema snapshot tests in `tests/test_compatibility_snapshots.py` pin every shape listed here and
fail CI if a change would break it — that failure is the enforcement mechanism, this document is
the policy it enforces.

"Frozen" means, concretely:

- an existing field is never renamed, removed, or repurposed to mean something else;
- an existing field's type never narrows or changes in an incompatible way;
- an existing event name, error `code`, or vocabulary entry never disappears or changes meaning;
- new fields, new event names, new operations, and new envelope schemas (`nctl.<command>.v2`)
  are always fine to add.

A breaking change to any of these is only made by minting a new major version alongside the old
one (`nctl.<command>.v2`, a new `/api/v2/` prefix) for a deprecation window — never by mutating
`v1`/`/api/v1/` in place.

## 1. `EventRecord` shape (`nctl_core.events.EventRecord`)

Frozen field set: `ts`, `operation_id`, `op`, `seq`, `event`, `level`, `message`, `data`. See
[event-log.md](event-log.md) for the meaning of each. New information goes into `data` or a new
`event` name, never a new top-level field with overlapping meaning.

## 2. Event vocabulary

The following event names are frozen in meaning (renames/removals require a documented major
bump; new names may be added freely):

- Core (any operation): `started`, `step_started`, `step_completed`, `warning`, `failed`,
  `finished`.
- `apply dnsmasq`: `rendered`, `setup_started`, `setup_completed`, `setup_dry_run_completed`,
  `dry_run_completed`, `apply_started`, `apply_completed`.
- `reconcile` (Phase 4): `plan_created`, `round_started`, `action_started`, `action_completed`,
  `actuation_completed`, `observation_completed`, `drift_resolved`, `non_converged`.
- observation/collection (shared by `reconcile` and `status`-adjacent flows):
  `collection_started`, `reports_retrieved`.

Full semantics for each are documented in [event-log.md](event-log.md); this list exists so a
removal or rename is mechanically detectable.

## 3. `nctl.<command>.v1` envelopes (`nctl_core.output.Envelope`)

The envelope wrapper itself (`schema`, `generated_at`, `ok`, `data`, `errors`) and
`EnvelopeError` (`code`, `message`, `detail`) are frozen. Each command's `data` payload is frozen
at its current field set and may only gain fields:

| Schema | `data` model |
|---|---|
| `nctl.status.v1` | `StatusData` (`nctl_core.status`) |
| `nctl.drift.v1` | `DriftData` (`nctl_core.drift_render`) |
| `nctl.dashboard.v1` | `DashboardData` (`nctl_core.dashboard_render`) |
| `nctl.apply.dnsmasq.v1` | `DnsmasqApplyData` (`nctl_core.dnsmasq_apply`) |
| `nctl.render.dnsmasq.v1` | `DnsmasqRenderData` (`nctl_core.dnsmasq_render`) |
| `nctl.render.production.v1` | `ProductionRenderData` (`nctl_core.production_render`) |
| `nctl.render.hosts_intent.v1` | `HostsIntentRenderData` (`nctl_core.hosts_intent_render`) |
| `nctl.reconcile.v1` | `ReconcileData` (`nctl_core.reconcile.executor`) |
| `nctl.ops.list.v1` | `OpsListData` (`nctl_core.ops_render`) |
| `nctl.ops.show.v1` | `OpsShowData` (`nctl_core.ops_render`) |
| `nctl.serve.v1` | `ServeData` (`nctl_core.serve.runtime`) |

A breaking change to any `data` shape above mints `nctl.<command>.v2` and keeps `v1` emitting
its current shape for a deprecation window; it does not alter `v1` in place.

## 4. HTTP/WS API surface (`/api/v1`)

Every path under `/api/v1/*` is subject to the same additive-only rule as the envelopes: methods
and response shapes for the paths below may gain fields/paths/methods but an existing
method+path combination never removes or repurposes what it returns.

Current frozen surface (see `nctl_core.serve.app.create_app`):

- `GET /api/v1/health` — the only unauthenticated endpoint under `/api/v1`.
- `GET /api/v1/status`, `GET /api/v1/drift` — latest persisted snapshots.
- `GET /api/v1/operations`, `POST /api/v1/operations` — list / create.
- `GET /api/v1/operations/{operation_id}` — one operation's record + terminal result.
- `GET /api/v1/operations/{operation_id}/events` — JSONL replay by `after_seq`.
- `GET /api/v1/operations/{operation_id}/artifacts`, `.../artifacts/{name}` — allowlisted
  artifact listing/fetch.
- `WS /api/v1/ws` — event stream (`EventRecord` frames, unchanged from the JSONL shape above).
  FastAPI's generated `/openapi.json` does not enumerate WebSocket routes, so this path is
  tracked here and in `tests/test_compatibility_snapshots.py` by name rather than via the
  OpenAPI snapshot.

`GET /`, `GET /openapi.json` are server-infrastructure routes, not part of the versioned API
contract, and are not subject to this freeze.

## Out of scope for this policy

Consistent with [p5/plan.md](../../devdocs/vision/core_reconcile/p5/plan.md) Step 6: this is a
policy freeze plus snapshot tests, not a JSON-Schema registry or codegen pipeline. Internal
module layout, CLI flag names, and config file shape are not covered — only the wire/file
contracts documented above.
