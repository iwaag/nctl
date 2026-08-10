# nctl

Unified CLI for [pj-clusterintent](https://github.com/iwaag/pj-clusterintent): computes desired/actual
drift and runs standard workflows. Implementation plan: `devdocs/big/core_reconcile/` in the parent repo.

## Layout

`src/nctl_core/` is the library; its business operations return typed models and
its CLI is only a presentation boundary. The following map is the ownership
index for package-level changes. “May import” states the allowed dependency
direction, not an obligation to import every listed package.

| path | owns | may import | must not import |
|---|---|---|---|
| `src/nctl_core/cli/` | Typer argument parsing and envelope presentation | orchestration/render modules | domain decisions, transport implementation details |
| `src/nctl_core/compute/` | typed compute rows, nintent-fixture-bound validation, source issue collection | stdlib and read-model contracts | transport clients, drift registration, planning, actuation |
| `src/nctl_core/drift/` | read-only comparison contracts, per-resource evaluation, status derivation | domain/read models; explicitly supplied snapshots | Nautobot clients, CLI, planning, actuation |
| `src/nctl_core/production/` | deterministic inventory contract, derivation, composition, route policy | domain contracts and supplied inputs | CLI, direct Nautobot I/O, action execution |
| `src/nctl_core/reconcile/` | plan construction, action DAG, bounded execution and durable evidence | drift outputs, production rendering, explicit transport/action adapters | CLI presentation or widened handler target sets |
| `src/nctl_core/reconcile/actions/` | one handler per reconciler ID and action-boundary translation | declared `ActionContext`, one explicit adapter | planner target selection, CLI, unrelated handlers |
| `src/nctl_core/sources/` | GraphQL queries and decoding into typed desired/actual snapshots | Nautobot transport and pure decode/validation helpers | drift policy, planning, actuation |

Modules over roughly 300 lines have an explicit owner as well:

| path | owns | may import | must not import |
|---|---|---|---|
| `reconcile/executor.py` | round lifecycle, dispatch, evidence retention | plans, action dispatch, rendering/adapters | resource-specific action policy |
| `production/contract.py` | production input/output schema and validation | stdlib, typed contract models | live transport or CLI |
| `dnsmasq.py` | pure dnsmasq desired export and deterministic bytes | read models and pure helpers | Nautobot, Ansible, CLI |
| `cli/main.py` | command wiring and exit/presentation boundary | public library builders/renderers | business rules |
| `production/composer.py` | composition of validated placement inputs into inventory/report | production contracts/derivation | direct source fetches or action execution |
| `ssh_enroll.py` | verified enrollment and managed known-hosts store semantics | SSH probes, artifacts, config | planner policy or CLI parsing |
| `sources/actual.py` | actual GraphQL query and typed decode | `NautobotClient`, pure models | drift/reconcile decisions |
| `sources/desired.py` | desired GraphQL query and typed decode | `NautobotClient`, compute decode helpers | drift/reconcile decisions |
| `desired_export.py` | desired-snapshot-to-batch-document projection and its fidelity policy | desired snapshot models, `NautobotClient`, envelope helpers | drift policy, planning, actuation, batch writes |
| `drift/endpoint_evaluation.py` | endpoint IPAM/range/interface/MAC evaluation | read models and pure drift helpers | transport, CLI, planning, actuation |
| `dnsmasq_apply.py` | direct dnsmasq apply boundary and its evidence | render result, SSH/Ansible adapters | drift classification or planner policy |
| `production/derivation.py` | pure operational-value derivation | production contract models | transport, CLI, execution |
| `compute/contract.py` | nintent-owned compute contract replay/validation | stdlib and compute models | transport, registry activation, actuation |
| `drift/comparators.py` | registered comparison functions and diff attribution | snapshots/evaluators/production composition | transport and reconcile execution |
| `observation.py` | nodeutils observation/ingest orchestration | explicit jobs/transport adapters | CLI or drift policy |
| `braindump.py` | braindump domain operations and authorization checks | explicit transport adapter/models | reconciliation decisions |
| `hosts_intent.py` | deterministic bootstrap inventory model/rendering | read models and pure helpers | transport, Ansible execution, CLI |
| `jobs.py` | Nautobot Job invocation/polling adapter | `NautobotClient`, transport models | domain policy or CLI |
| `reconcile/planner.py` | drift-to-action planning and exact target set | diffs, reconcilers, snapshots | handler execution or CLI |
| `drift/node_evaluation.py` | node realization and candidate ranking evaluation | read models and pure drift helpers | transport, CLI, planning, actuation |
| `reconcile/ledger.py` | explicit ledger mutation adapter | Job/transport adapter and typed requests | planner policy or CLI |
| `reconcile/ssh_preflight.py` | exact-generation SSH preflight | resolved routes and SSH adapter | target selection or action policy |
| `drift/evaluation_snapshot.py` | snapshot-to-evaluator coordination | evaluators and supplied snapshot | transport, CLI, planning, actuation |

The shared compute contract is semantically owned by nintent. nctl retains read-time validation so
stale or compromised GraphQL rows become visible source issues, but its behavior is fixture-bound:
`nctl_core.compute.contract` and `nctl_core.compute.collection` replay nintent's generated
`tests/fixtures/compute_conformance.json`. Run the superproject's
`devtests/test_strategy/test_compute_conformance.py` freshness gate whenever either side changes;
nctl never imports nintent at runtime.

## Setup

```bash
uv sync
cp example.nctl.toml ../nctl.toml   # at the pj-clusterintent root, git-ignored
export NAUTOBOT_TOKEN=...           # or set nautobot.token_file
```

## Recipes

- [`register a new PC`](docs/register-a-new-pc.md) — new machine to converged, intent-only.
- [`add a basic service`](docs/add-a-basic-service.md) — place a service on an existing node.
- [`add / retire a Proxmox LXC guest`](docs/add-and-retire-proxmox-lxc.md) — compute-backed guest
  creation and destruction workflow.
- [`state bundle`](docs/state-bundle.md) — the cluster's desired/actual state as one downloadable
  zip (`nctl.bundle.v1` manifest convention and the compose-and-upload recipe).
- [`hand-write a partial batch`](docs/desired-partial-batch.md) — minimal `desired apply`
  document template and the identity-key members per kind.

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
uv run nctl relations
uv run nctl relations --json
uv run nctl apply dnsmasq
uv run nctl apply dnsmasq --yes
uv run nctl reconcile
uv run nctl reconcile agstudio
uv run nctl reconcile agstudio --yes
uv run nctl reconcile RETIRED_GUEST --allow-destroy
uv run nctl reconcile RETIRED_GUEST --allow-destroy --yes
uv run nctl reconcile --yes --max-rounds 1 --json
uv run nctl desired apply -f .local/desired-state.yaml
uv run nctl desired apply -f .local/desired-state.yaml --yes
uv run nctl desired export > snapshot.yaml
uv run nctl desired export -o .local/desired-state.yaml
uv run nctl desired export --json
uv run nctl ops list
uv run nctl ops list --limit 5 --json
uv run nctl ops show 01KXPYQRJ8GTNND0PC3KZSMPXC
uv run nctl ops show 01KXPYQRJ8GTNND0PC3KZSMPXC --after-seq 3 --json
uv run nctl upload state.json
uv run nctl upload state.json --ttl 2h --json
uv run nctl upload report.md evidence/ --zip
uv run nctl braindump list
uv run nctl braindump show <braindump-id>
uv run nctl braindump create --title "Home lab" --authorship user_direct --body "Keep Ollama on agpc."
uv run nctl braindump create --title "Home lab" --authorship user_direct --file wish.txt
uv run nctl braindump review <braindump-id> --summary "agpc already runs Ollama; no drift."
uv run nctl braindump review-delete <braindump-id> --yes
```

`status` checks Nautobot connectivity/auth/intent-catalog presence, Celery worker health, nodeutils
dump freshness, and parent-repo submodule state. Each check degrades independently: e.g. an
unreachable Nautobot still yields dump and submodule info, with `ok: false` and an entry in
`errors`. The worker check (no_guest_vm G1) reads two REST signals: `celery-workers-running` from
`/api/status/` (a dead worker → `celery_workers_not_running`), and PENDING JobResults older than
120s while a worker is registered (`worker_queue_stalled` — the silent-stall mode where the worker
still answers pings but its consumer connection is dead; the recorded fix is a worker container
restart).

`render dnsmasq` fetches desired endpoints, IP ranges, and actual node/interface state through GraphQL and
prints a deterministic dnsmasq configuration. Its exact UTF-8 bytes (header plus sorted directives)
have a `content_sha256`; timestamps and operation IDs are envelope metadata, never deployed bytes.
Use `--out PATH` to write the configuration or `--json` to inspect the complete render payload.

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

`drift` and `relations` compute the current desired/actual/observed picture as read-only queries;
see [`docs/drift-and-relations.md`](docs/drift-and-relations.md) for envelope shape, target status
vocabulary, and the status legend.

`apply dnsmasq` — plan/apply semantics, content-drift checks, and the `--inventory` bootstrap
escape hatch for a freshly registered node — is covered in
[`docs/apply-dnsmasq.md`](docs/apply-dnsmasq.md). Routine dnsmasq changes should go through
`reconcile --yes` rather than a direct apply; see [`docs/reconcile.md`](docs/reconcile.md).

`reconcile` — the routine, single-command path from drift to a freshly verified converged state —
has its own [`docs/reconcile.md`](docs/reconcile.md): plan/apply modes, per-round action ordering,
the `nctl.reconcile.v2` envelope, `[reconcile]` config, SSH trust preflight, IPAM eligibility, and
`--refresh-observation`.

`lifecycle` (direct `DesiredNode.lifecycle` setter) and `desired export` (canonical re-applyable
desired-state backup) are covered in
[`docs/lifecycle-and-desired-export.md`](docs/lifecycle-and-desired-export.md).

`ops list`/`ops show` (read-only operation history), `upload` (presigned-URL file/zip upload), and
`braindump` (the typed wish/alignment-review diary) are covered in
[`docs/ops-upload-braindump.md`](docs/ops-upload-braindump.md).

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

## SSH trust configuration

```toml
[ssh]
known_hosts_file = "~/.local/state/nctl/ssh/known_hosts" # default
keyscan_timeout_seconds = 10                              # default
lock_path = "~/.local/state/nctl/ssh.lock"                # default
```

`[ssh]` configures nctl's own managed known_hosts store, keyed by a stable per-node
`HostKeyAlias`, that backs the enroll → observe → reconcile trust lifecycle, hardware
replacement/key rotation, and every direct-Ansible fail-closed check. See
[`docs/ssh-trust.md`](docs/ssh-trust.md) for the full contract and recovery procedure.

## Conventions

- **Config**: `nctl.toml`, resolved as `--config` → `$NCTL_CONFIG` → `./nctl.toml` → parent-repo root.
  Tokens are never stored in the file (rejected by validation); use `token_env` / `token_file`.
- **JSON output**: every command returns a stable `nctl.<command>.v1` envelope via `--json`
  (spec: `docs/output-format.md`).
- **Event logs**: long-running operations emit JSON Lines with an operation ID
  (spec: `docs/event-log.md`).
- **Exit codes**: 0 ok / 1 command failure / 2 usage or config error.
- **Reads vs writes**: reads go through Nautobot GraphQL (`NautobotClient.graphql()`, a single
  unified client for both core DCIM/IPAM and `nintent`'s desired-state types); writes stay REST.
  Nautobot GraphQL is read-only, and every structured desired-state write uses the single
  `POST /api/plugins/intent-catalog/desired-state/batch/` endpoint rather than an intent-catalog
  ViewSet.

See [`docs/extending.md`](docs/extending.md) for the module admission checklist and the guides for
adding a drift comparator or a reconciler.

## Development

```bash
uv run pytest -q --durations=20
```

See the repository [test strategy command matrix](../README_DEV.md#test-strategy-command-matrix)
for required conformance gates, prerequisites, and cleanup ownership.
