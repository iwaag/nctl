# Event log format

Long-running operations (and `status`, to exercise the convention end to end even though it's
short) emit one JSON Lines file per run via `nctl_core.events.OperationLog`:

```
<log_dir>/<operation_id>.jsonl
```

`log_dir` comes from `[events].log_dir` in `nctl.toml` (default `~/.local/state/nctl/events`).

## Record shape

One JSON object per line:

```json
{"ts": "2026-07-14T12:00:00.123+00:00", "operation_id": "01J...", "op": "status", "seq": 0, "event": "started", "level": "info", "message": "status started", "data": {}}
```

- `ts` — ISO 8601 UTC timestamp with millisecond precision.
- `operation_id` — a ULID (sortable by time, unique). Also included in the command's JSON
  envelope as `data.operation_id`, so a log file and its command output can be cross-referenced.
- `op` — the command name (`status`, later `reconcile`, `apply dnsmasq`, ...).
- `seq` — 0-based, monotonically increasing per operation; gaps mean lost events, never reordering.
- `event` — one of the core vocabulary below, or an op-specific extension.
- `level` — `info` | `warning` | `error`.
- `message` — human-readable summary.
- `data` — free-form extra fields specific to the event (passed as `**kwargs` to `emit`).

## Core event vocabulary

- `started` — emitted automatically by `OperationLog.start(op, log_dir)`.
- `step_started` / `step_completed` — bracket an independent unit of work within the operation.
- `warning` — a non-fatal issue worth surfacing.
- `failed` — a step or the operation failed.
- `finished` — emitted by `OperationLog.finish(ok=...)`; always the last record, carries `data.ok`.

Later phases add operation-specific events (e.g. `drift_resolved` for `reconcile`) within this
same shape — the vocabulary is extensible per-op via `data`, not a closed enum.

`apply dnsmasq` currently adds:

- `rendered` — the operation-specific configuration artifact was written.
- `dry_run_completed` — the default Ansible check+diff run exited successfully.
- `apply_started` / `apply_completed` — bracket the real Ansible run selected by `--yes`.
- `failed` — rendering, validation, inventory resolution, or Ansible execution failed; followed by
  the final `finished` record with `ok: false`.

## `nctl reconcile` event vocabulary (Phase 4)

`reconcile` (Step 5 onward) adds the following events on top of the core vocabulary above:

- `plan_created` — a `nctl.reconcile.plan.v1` plan was built for the round; `data` carries the
  plan's `drift_fingerprint` and action count. Emitted in both plan and apply mode.
- `round_started` — a bounded re-plan round begins; `data.round` is the 0-based round number.
- `action_started` / `action_completed` — bracket one planned action's execution (ledger PATCH,
  Job trigger, or Ansible playbook run). `action_completed` carries `data.success` and the
  action's target list, independent of whether the action requires fresh observation afterward.
- `actuation_completed` — a stricter subset of `action_completed`, emitted only for actions that
  mutate host/service state and may leave the affected targets in a transient "change in flight"
  state. This is the **only** event the `converging` status rule
  (`nctl_core.drift.status.derive_status`, `nctl_core.drift.operations`) reads, and it reads
  `data` structurally rather than scanning for a slug:
  - `data.target_slugs` — the exact desired-node/service slugs this actuation affects;
  - `data.claimed_diff_codes` — the diff codes this actuation claims to resolve; an error diff
    with any other code keeps its target `drifting`/`unknown` even while this event is pending
    fresh observation;
  - `data.requires_observation` — `true` only when a later `observation_completed` is needed to
    confirm the result; ledger-only actions (which nctl reverifies immediately by refetching)
    must omit this or set it `false`, so they never produce `converging`;
  - `data.success` — `true` only for a successful actuation. A failed or cancelled actuation, or
    any `actuation_completed` for the same targets emitted later with `success: false`,
    supersedes an earlier success and must not leave it `converging` — only the chronologically
    latest `actuation_completed` naming a target is consulted.
- `observation_completed` — a fresh nodeutils collection/ingest cycle finished; `data.ok` reports
  whether the refreshed actual state was successfully retrieved (see `nctl_core/observation.py`).
- `drift_resolved` — every selected target reached `converged` in a round's post-actuation drift
  check.
- `non_converged` — the operation stopped without full convergence (manual/unsupported block,
  unchanged fingerprint, or max-rounds reached); `data` carries the stop reason.

`converging` is never derived from `step_started`/`step_completed` or any other event that merely
happens to mention a slug in free-form `data` — that scanning behavior was Phase 2 scaffolding and
was removed in Phase 4 Step 4 precisely because `reconcile` itself now emits many events that
would otherwise make unrelated drift look like change in flight.

## API

```python
op = OperationLog.start("status", log_dir)
op.emit("step_started", "checking nautobot")
op.emit("step_completed", "nautobot checked", ok=True)
op.finish(ok=True)
```

A failure to write the log file (permissions, missing parent that can't be created, etc.) prints
one warning to stderr and is otherwise swallowed — it must never crash the command it instruments.
