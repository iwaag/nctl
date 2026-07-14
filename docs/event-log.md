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

## API

```python
op = OperationLog.start("status", log_dir)
op.emit("step_started", "checking nautobot")
op.emit("step_completed", "nautobot checked", ok=True)
op.finish(ok=True)
```

A failure to write the log file (permissions, missing parent that can't be created, etc.) prints
one warning to stderr and is otherwise swallowed — it must never crash the command it instruments.
