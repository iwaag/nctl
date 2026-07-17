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

`apply dnsmasq` renders into an operation-specific artifact and invokes the deploy-only Ansible
playbook in `--check --diff` mode by default. Review that output, then use `--yes` for the real
apply. The configured inventory must resolve at least one host in `dnsmasq_server`; an existing
inventory file with an empty or missing group is rejected instead of succeeding as a no-op.

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
status summaries, and the `dashboard` result. The plan itself
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

`nctl reconcile --yes` is the routine entry point that replaces the old
`bootstrap-inventory` → `collect_nodeutils_and_ingest_nautobot.yml` → `production-inventory`
Ansible/Makefile sequence; `ansible_agdev/Makefile`'s `pipeline` target now runs exactly this
command. `make bootstrap-inventory`/`make production-inventory` remain as standalone diagnostics —
`reconcile` renders its own operation-scoped bootstrap inventory and regenerates the full production
inventory itself, so it never shells out to either.

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
