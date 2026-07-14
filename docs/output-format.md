# JSON output envelope

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
