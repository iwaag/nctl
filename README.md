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
uv run nctl render production
uv run nctl render production --out ../ansible_agdev/inventories/generated
uv run nctl drift
uv run nctl drift --host agstudio --json
uv run nctl apply dnsmasq
uv run nctl apply dnsmasq --yes
```

`status` checks Nautobot connectivity/auth/intent-catalog presence, nodeutils dump freshness, and
parent-repo submodule state. Each of the three checks degrades independently: e.g. an unreachable
Nautobot still yields dump and submodule info, with `ok: false` and an entry in `errors`.

`render dnsmasq` fetches desired endpoints, IP ranges, and actual node/interface state through GraphQL and
prints a deterministic dnsmasq configuration. Use `--out PATH` to write the configuration or
`--json` to inspect the complete render payload.

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
