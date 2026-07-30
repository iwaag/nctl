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
uv run nctl reconcile
uv run nctl reconcile agstudio
uv run nctl reconcile agstudio --yes
uv run nctl reconcile RETIRED_GUEST --allow-destroy
uv run nctl reconcile RETIRED_GUEST --allow-destroy --yes
uv run nctl reconcile --yes --max-rounds 1 --json
uv run nctl ops list
uv run nctl ops list --limit 5 --json
uv run nctl ops show 01KXPYQRJ8GTNND0PC3KZSMPXC
uv run nctl ops show 01KXPYQRJ8GTNND0PC3KZSMPXC --after-seq 3 --json
uv run nctl braindump list
uv run nctl braindump show <braindump-id>
uv run nctl braindump create --title "Home lab" --authorship user_direct --body "Keep Ollama on agpc."
uv run nctl braindump create --title "Home lab" --authorship user_direct --file wish.txt
uv run nctl braindump review <braindump-id> --summary "agpc already runs Ollama; no drift."
uv run nctl braindump review-delete <braindump-id> --yes
```

`status` checks Nautobot connectivity/auth/intent-catalog presence, nodeutils dump freshness, and
parent-repo submodule state. Each of the three checks degrades independently: e.g. an unreachable
Nautobot still yields dump and submodule info, with `ok: false` and an entry in `errors`.

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
instead of succeeding as a no-op. Direct `apply dnsmasq` always targets the whole `dnsmasq_server`
group; a `reconcile`-driven `dnsmasq_config` action instead scans, deploys, and re-observes only its
exact planned host set (`fix_sshkey4` Step 3), so a host-scoped reconcile can never actuate a
sibling placement it never scanned. The deployed destination path is resolved exactly once from
validated `deployment_profile_reconciliation` metadata and passed to the playbook as a structured
extra-vars payload -- it is never a literal the playbook constructs itself. Content drift also
checks the observed managed-file path and digest algorithm, not only the digest
(`service_config_observation_mismatch`): a digest match at the wrong reported path plans a fresh
observation rather than a blind deploy.

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

`apply dnsmasq --yes` remains useful for a reviewed, direct deployment. Routine DNS, DHCP
reservation, and DHCP-range intent changes should use `nctl reconcile --yes`: a running daemon
with a mismatching managed-file digest is real `service_config_mismatch` drift and is re-observed
after deployment. The content contract covers only nctl's
`/etc/dnsmasq.d/nintent-records.conf`, not every dnsmasq package default or `ansible.conf` setting.

`nctl drift` is the supported way to get the current drift picture: it is a side-effect-free read
computing fresh desired-versus-actual status on every call, printed as human text or (`--json`)
the `nctl.drift.v1` envelope. It never writes a file and never pushes anything back into nintent —
there is no separate regeneration command to remember to run instead.

### Status legend

`nctl drift` targets use one status vocabulary:

| status | meaning |
|---|---|
| `converged` | no error-severity diffs |
| `converging` | diffs exist, but a newer `apply`/`reconcile` operation targets this node than its latest actual observation — change is in flight |
| `drifting` | an error-severity diff exists and nothing in flight explains it |
| `unknown` | required actual data is missing, stale, or never linked — nctl cannot see this target, which is different from it having drifted |

For a specific bounded operation's outcome, use its `result.json` (embedded in `nctl reconcile`
output, or read later via `nctl ops show`) rather than re-deriving it from a separately cached
status. For historical operations, `nctl ops list`/`nctl ops show` read past and running
operations directly from the on-disk event-log directory — they are operation history, not a
live convergence cache, so always cross-check a historical result against a fresh `nctl drift` if
the current state matters.

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
- **Audit trail**: before `--yes` mutates anything, nctl verifies the operation directory and event
  log are writable and refuses to proceed if they aren't (`artifact_write_failed`) — a mutating run
  never proceeds without a place to record what it did.

The `nctl.reconcile.v2` envelope's `data` carries `operation_id`, `mode`, `scope`, terminal `state`
(`planned | already_converged | converged | manual_intervention_required | non_converged | failed`),
`event_log_path`, `artifact_dir`, `plan_path`, initial/final drift paths, per-round action results
(`rounds`), `manual_review`/`unsupported` records (target + diff code + evidence), scope/global
status summaries, and `ssh_preflight` (below). The plan itself
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
# Source checkouts normally omit this: nctl resolves the superproject's
# pinned nodeutils gitlink. Packaged controllers may set the same full SHA.
# nodeutils_version = "0123456789abcdef0123456789abcdef01234567"
```

Every observation passes an exact `nodeutils_version` commit to Ansible. In a
normal source checkout this is the `nodeutils` gitlink recorded by the
superproject commit, not the mutable GitHub `HEAD` and not whatever commit
happens to be checked out in the submodule working tree. This keeps the remote
collector schema coordinated with the local nctl reader. The resolved SHA is
recorded in the operation event log and observation action result. If the
superproject gitlink cannot be resolved, observation fails before Ansible
runs; it never falls back to `HEAD`.

**SSH trust preflight** (`devdocs/small/fix_sshkey/plan.md` Step 5): every round, before observation,
Nautobot Jobs, inventory writes, or playbooks run, nctl checks that every node touched by an
SSH-requiring action (`observe_node`, `service_profile`, `dnsmasq_config`) has at least one entry
under its stable alias in `[ssh].known_hosts_file`. A missing entry fails the whole round with
`ssh_host_key_unenrolled` and the verified-source `nctl ssh enroll <slug>
--from-known-hosts`/`--fingerprint` remediation, before any write --
this is what prevented the original incident (`agdnsmasq`'s observation/IPAM succeeding, then
failing on the production dnsmasq SSH connection). Ledger-only actions (`link_actual_node`,
`reconcile_ipam`) never require enrollment, so an unrelated unenrolled host never blocks them.

**IPAM eligibility is policy-aware, not `ip_policy=dhcp_reserved`-only** (ipam_policy plan): an
endpoint with a `dhcp_reserved` IP is always automatic (`missing_actual_ip_address`/
`actual_ip_address_not_linked` -> `reconcile_ipam`), matching prior behavior. A `static`/`external`
endpoint is automatic only when its linked realized Device's observed `primary_ip_address` matches
the desired host; otherwise drift reports one of `ipam_reconcile_observation_missing`/`_mismatch`/
`_ambiguous` as a `manual_review` finding instead of silently repeating the same unresolved action
every round. IPAM ledger reconciliation only creates/links the Nautobot `IPAddress` object when
both an explicit desired IP and a matching self-observation already exist; it never actuates the
host's own IP configuration and never assigns an `IPAddress` to an `Interface`.
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

`nctl reconcile HOST --refresh-observation` adds one explicit `observe_node`
action to the first round even when the current drift is already converged.
Without `--yes` it is a dry plan; with `--yes` it updates nodeutils to the
superproject-pinned commit, collects and ingests one fresh report, then returns
to the ordinary round planner so the refresh is not repeated indefinitely.
Host scope is required to prevent an accidental whole-cluster observation.

### `lifecycle`

`nctl lifecycle NODE STATE [--json]` is a direct, idempotent setter for one `DesiredNode`'s
`lifecycle` (`planned`, `approved`, `active`, `deprecated`, `retired`) — it is **not** an approval
engine and is not part of `reconcile --yes`; nothing in `reconcile` changes lifecycle automatically.
Nodes are created `active` by default (Better Usability Phase 3), so this command exists for
deliberate staging/promotion/demotion, not routine registration.

`NODE` must be an exact desired-node slug. The command resolves it through the same GraphQL read
path as every other command, no-ops (`changed: false`, no write) if the node is already in the
requested state, otherwise submits a one-operation desired-state batch that changes only
`{"lifecycle": STATE}`, then refetches through GraphQL and fails closed
(`lifecycle_confirmation_mismatch`) unless the write is confirmed. Text output is `NODE: before -> after` or `NODE: already STATE (no
change)`; `--json` prints the closed `nctl.lifecycle.v1` envelope (`node_id`, `node_slug`,
`previous_state`, `requested_state`, `current_state`, `changed`). `unknown_node` and
`invalid_lifecycle` are usage exits (2); a rejected PATCH or confirmation mismatch is a failure exit
(1) with no success claim. No new drift/reconcile classification code is introduced — promoting a
node only makes it eligible for whatever findings already applied to `active`/`approved` nodes.

### `ops list` / `ops show`

`nctl ops list [--limit N] [--json]` and `nctl ops show OPERATION_ID [--after-seq N] [--json]` are
a read-only, filesystem-only view over `[events].log_dir` — no live process, Nautobot, or Ansible
access required. `ops list` enumerates every `<operation_id>.jsonl` file, newest first, parsing
just enough of each to report `op`/`state`/`ok`/`result`/timestamps (schema `nctl.ops.list.v1`).
`ops show` additionally returns the full event list (or only events with `seq > --after-seq`) plus
the resolved `artifact_dir` and its artifact list, using the same corrupt-line-tolerant JSONL
reader (schema `nctl.ops.show.v1`; a truncated or partially written final line is reported via
`corrupt_lines`, not raised as an error). `nctl_core.operations_index` is a retained CLI-only
helper: the JSONL event log and operation-artifact directories are durable disk evidence read by
this module and the CLI alone, not published to or consumed by any external subscriber.

### `braindump`

`nctl braindump {list,show,create,supersede,review,review-delete}` is the deterministic,
typed interface to the exchange diary described in `devdocs/big/braindump/roadmap.md`: a
**Braindump** is the user's free-form wish, and its at-most-one current **Alignment Review** is the
AI agent's latest natural-language reply. Neither is executable input, and this command surface has
no import path into `drift`, `reconcile`, Jobs, nodeutils, or Ansible — reading or
writing the diary never changes convergence status or triggers actuation.

- `list [--include-superseded] [--json]` / `show ID [--json]` read through GraphQL only and never write. `list` returns only active documents by default; `--include-superseded` explicitly includes reference-only history. It returns a
  compact `id`/`title`/`authorship`/timestamps/review-presence/attention projection; `show` returns
  the full record including `body` and, if present, the review's `summary`.
- `create --title TITLE --authorship AUTHOR (--body TEXT | --file PATH)` writes through REST
  and always confirm the result via a fresh GraphQL refetch before reporting success; a mismatch is
  a command-scoped `*_confirmation_mismatch` failure, never a fabricated success. `AUTHOR` is
  exactly `user_direct` or `agent_transcribed` — there is no default, so provenance is never
  misstated.
- `supersede --old OLD_ID [--old OLD_ID ...] --title TITLE --authorship AUTHOR (--body TEXT | --file PATH)` is the only status transition. It atomically creates the active replacement and marks exactly the selected active old documents `superseded`; any validation failure leaves all old rows active and retains no replacement.
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
- There is no Braindump delete command. `review-delete ID [--yes]` deletes only the review, returning the Braindump to the unreviewed
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

The store has one strict reader (`devdocs/small/fix_sshkey4/plan.md`): an absent file is a valid
empty store (`unenrolled` for every host), and every other non-blank/non-comment line must be a
supported bare-alias entry or it is corruption. A missing enrollment and an invalid managed store
are always distinguishable -- corruption never falls back to `unenrolled` -- and every public
boundary (`ssh enroll`, `apply dnsmasq`, reconcile's pre-round gate, bootstrap and post-actuation
observation inside a started round) reports it as a structured `ssh_store_read_failed` result
rather than an uncaught exception. A store failure that occurs after a round has already recorded
progress never discards that progress: the round, its completed actions, and a freshly refreshed
final drift are retained, with `final_drift_unknown` recorded if even that refresh fails. A
syntactically valid historical `[nctl-node-UUID]:port` entry is recognized separately as migration
residue -- it never satisfies current enrollment and is removed only by a subsequent verified
enrollment/replacement write, never automatically.

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
post-regeneration SSH scan uses only a `ResolvedSshTarget` from the exact generation just composed
and written, never a snapshot fetched earlier in the same round, and a production route that cannot
be resolved for a target fails closed
(`no_resolvable_production_route`) rather than falling back to mDNS -- mDNS selection is reserved
for the bootstrap phase, which is the only phase guaranteed to still use it.

The supported nctl inventory contract rejects policy-changing SSH variables (including
`ansible_ssh_args`, extra-argument variables, custom SSH executables, non-SSH connections, and
host-key-checking overrides) and accepts only an integer port in `1..65535`. Direct Ansible commands
with hostile CLI options or environment variables are outside that supported path.

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

### Module admission

A module is admitted for a responsibility, not to make another file shorter. Every new or split
module must:

1. own one operational value, contract, target set, route, identity, or lifecycle decision;
2. have a reason to change independent of the module it was separated from;
3. name its consumers;
4. belong to exactly one layer — transport, domain, orchestration, or presentation — and follow
   the dependency direction `presentation → orchestration → domain ← transport` without importing
   downward across it;
5. not exist solely to reduce line count;
6. not recreate a public schema deleted as an internal abstraction; and
7. have a documented entry in this README's responsibility map.

An interface needs either two current implementations, or one current implementation and a second
one named in an approved roadmap. Treat line count only as a prompt to inspect ownership; it is
never the reason to split by itself.

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

Compute realization is an active comparator example: `drift/compute_evaluation.py` owns the pure
platform/guest matching and field-comparison decision, while the thin `compute_instance`
registration in `comparators.py` attaches it to drift. Phase 1 deliberately classifies every
compute finding as manual review or unsupported: the evaluator may derive a candidate, but it
derives a unique existing guest candidate. A Phase 2 `ledger_patch` action may
record that candidate through the narrow compute-link API. A fully preflighted absent LXC is
instead planned as one `create_compute_instance` action, pinned to its control host and exact
`pct create` grammar; its handler re-derives those values and invokes only the bounded Proxmox
create playbook. Dry plans never invoke that handler or mutate a Proxmox guest.

## Adding one Proxmox LXC guest

### Guest OS and compute are separate realization layers

For a compute-backed guest, the Nautobot **Device** represents the managed guest OS: it is the
`DesiredNode.realized_device` target and owns nodeutils facts, observed services, operational
derivation, and node-level IPAM evidence. The Nautobot **VirtualMachine** represents the Proxmox
compute resource: it is the `DesiredComputeInstance.realized_vm` target and owns VMID, guest
kind, capacity, power, interfaces, and Proxmox realization. Both objects may legitimately
describe one guest; a VirtualMachine never replaces the Device link for guest-OS realization.
See [`devdocs/big/vm/roadmap.md`](../devdocs/big/vm/roadmap.md) for the detailed contract.

This is the ordinary, bounded workflow for an approved LXC guest; it is not a generic Proxmox
lifecycle interface.

1. Record a user-confirmed Braindump wish, then write the exact desired node, one primary endpoint
   (static IPv4 CIDR, same-subnet gateway, and explicit MAC), compute platform, VMID, LXC template, rootfs storage, bridge,
   resources, and desired running state through nintent's canonical writer. A prose wish alone
   cannot plan or create a guest.
2. Run `nctl reconcile GUEST --json` without `--yes`. The plan must name only that guest and its
   pinned control host. Its preflight requires fresh platform evidence, the exact template,
   storage, and bridge, one NIC-bearing endpoint, and no desired or actual VMID/MAC/IP collision.
   A missing template, collision, stale ledger, ambiguous endpoint, or unreachable platform is a
   stop to correct or refresh evidence; do not bypass it with direct `pct` commands.
3. After reviewing the unchanged plan, run `nctl reconcile GUEST --yes` under separate apply
   authority. The registered handler uses the existing strict SSH trust and only the bounded
   `ansible_agdev/playbooks/proxmox/create_lxc.yml` adapter (`pct status`, `pct create`, and
   `pct start`). It cannot stop, delete, resize, replace, migrate, clone, or create QEMU guests.
4. Reconcile re-observes and ingests the guest, then links the exact realized VirtualMachine. If
   creation succeeded but observation or identification failed, treat the operation evidence as
   partial progress: find the recorded VMID, refresh observation, and link the identified guest.
   Never submit a second create as recovery.
5. A newly linked LXC without guest OS access reaches
   `waiting_for_manual_initial_access`. The static IPv4 CIDR and gateway are already configured by
   `pct create`; use the Proxmox console for guest user, key, privilege, SSH, and mDNS setup;
   complete normal node observation and enrollment afterward.
   Until then it is intentionally excluded from production inventory. A repeat dry plan must not
   create, start, or link it again.

### Retiring one Proxmox LXC

Braindump text and its supersession status are never reconciliation input. After the user confirms
the exact target, submit one canonical desired-state batch that sets the owning `DesiredNode` to
`retired` and that `DesiredComputeInstance.desired_presence` to `absent`. Do not treat an omitted
Desired row, an unmanaged guest, or a missing observation as deletion intent.

1. Run `nctl reconcile GUEST --json` without `--yes` and review the one pinned
   `destroy_compute_instance` action: it must name the expected LXC VMID and its exact Proxmox
   control node.
2. `nctl reconcile GUEST --allow-destroy` remains a dry plan. `nctl reconcile GUEST --yes` refuses
   the action with `destroy_capability_not_enabled`; neither command reaches Proxmox.
3. Only after reviewing the same target, run `nctl reconcile GUEST --allow-destroy --yes`. The
   handler re-derives the disposition before mutation and invokes only the bounded
   `ansible_agdev/playbooks/proxmox/destroy_lxc.yml` adapter for that VMID and control node.
4. Reconcile performs its normal control-node observation and Nautobot ingest. It completes only
   when fresh drift observes the retained VirtualMachine with `proxmox_presence=absent`. If
   destruction succeeded but that observation fails, retain the operation evidence and refresh
   observation; do not submit a second destroy blindly.

This removes only the planned LXC. It does not delete Braindumps, Desired rows, VirtualMachine
rows, or Device rows, and it does not support QEMU, wildcard targets, schedules, or general
provider disposal.

## Adding a reconciler

Adding a reconciler changes the bounded plan/apply contract, so make each ownership point explicit:

1. Declare one stable `Reconciler` in `reconcile/reconcilers.py` through
   `reconcile/registry.py::register_reconciler()`. Its ID, default mutation posture, and action kind
   participate in the deterministic action DAG; add any dependency wiring where the planner builds
   the action.
2. Classify only its owned drift codes in `reconcile/classify.py`, then have
   `reconcile/planner.py::build_plan()` construct the `ReconcileAction`. The planner owns the exact
   target set. A handler consumes `action.targets` and must never widen it or substitute a convenient
   inventory group.
3. Implement one handler under `reconcile/actions/`. It receives `ActionContext` and returns
   `ExecutedAction`; use `ActionHandler` metadata to declare its `phase` (`bootstrap` or `service`)
   and whether it `needs_client`.
4. Register that handler in `reconcile/actions/dispatch.py`'s dispatch table. Keep expected
   `LedgerActionError`, `NautobotJobError`, and `NautobotError` translation there, where their code,
   mutation state, and durable action evidence are converted at the public action boundary.

Add focused planner, handler, and executor evidence tests. Do not create a placeholder reconciler,
handler, or dispatch entry: an inactive implementation is not a safe extension point.

## Development

```bash
uv run pytest -q --durations=20
```

See the repository [test strategy command matrix](../README_DEV.md#test-strategy-command-matrix)
for required conformance gates, prerequisites, and cleanup ownership.
