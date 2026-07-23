# Usage examples: common instructions -> commands

A quick lookup from "what someone asked for" to the exact `nctl` command to run. All commands
below assume `uv run --project nctl` prefixing `nctl`.

| Instruction | Command |
| --- | --- |
| "Plan a refresh of NODE's actual state" | `nctl reconcile NODE` (reads current state and plans; nodeutils does not run yet) |
| "Refresh NODE's actual state / re-run nodeutils on NODE" | `nctl reconcile NODE --refresh-observation` (review), then add `--yes` |
| "Actually apply what's pending on NODE" | `nctl reconcile NODE --yes` |
| "Refresh and apply for the whole cluster" | `nctl reconcile` / `nctl reconcile --yes` (no host argument) |
| "What's the current drift on NODE?" | `nctl drift --host NODE` |
| "What's the drift on a specific service?" | `nctl drift --service SERVICE` |
| "Give me the overall cluster status" | `nctl status` |
| "Regenerate the dashboard" | `nctl dashboard` |
| "Mark NODE as planned/approved/active/deprecated/retired" | `nctl lifecycle NODE STATE` |
| "Trust this SSH host key for NODE" | `nctl ssh enroll NODE --from-known-hosts` (add `--yes` to write) |
| "Render dnsmasq config" | `nctl render dnsmasq` |
| "Deploy dnsmasq config" | `nctl apply dnsmasq --yes` |
| "Render the production inventory" | `nctl render production` |
| "Render the bootstrap hosts_intent inventory" | `nctl render hosts-intent` |
| "List recent operations" | `nctl ops list` |
| "Show one operation's detail" | `nctl ops show OPERATION_ID` |
| "Show/update the Braindump diary" | `nctl braindump list` / `nctl braindump show ID` / `nctl braindump create ...` |
| "Start a new agent session for a task" | `nctl session new TASK_NAME --topic TOPIC` |
| "Serve the live dashboard/API" | `nctl serve` |

## nodeutils specifically

There is no standalone "run nodeutils on this host" command. Ordinary
`nctl reconcile NODE --yes` runs nodeutils when drift requires fresh
observation. To request a refresh even when the node is already converged,
use the explicit refresh flag:

```bash
uv run --project nctl nctl reconcile NODE --refresh-observation       # dry plan
uv run --project nctl nctl reconcile NODE --refresh-observation --yes # collect, ingest, re-plan
```

`NODE` is the DesiredNode **slug** (as recorded in Nautobot), not an IP or bare hostname.

Before the first SSH-requiring operation for a node (and after deliberate
loss of the nctl-managed trust store), enroll its stable alias from a verified
source:

```bash
uv run --project nctl nctl ssh enroll NODE --from-known-hosts       # inspect only
uv run --project nctl nctl ssh enroll NODE --from-known-hosts --yes # write after review
```

For a new machine without an existing trusted `.local` entry, use an
out-of-band verified `--fingerprint SHA256:...` instead. `reconcile NODE`
reports `ssh_preflight: unenrolled=[NODE]` when this prerequisite is missing;
do not bypass it with disabled host-key checking.

The supported reconcile path deploys the exact `nodeutils` commit pinned by
the pj-clusterintent superproject. It does not follow the mutable GitHub
`HEAD`, so a future collector schema change cannot race ahead of the nctl
reader during a normal observation.

For diagnostics only (no Nautobot write), the underlying playbook can be run directly:

```bash
ansible-playbook playbooks/nautobot/run_nodeutils_collect.yml --limit NODE
```

This writes a local inventory report and does not ingest anything.
