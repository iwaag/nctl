# `drift` and `relations`

See [`../README.md`](../README.md#usage) for the full command list.

## `drift`

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

`nctl drift` is the supported way to get the current drift picture: it is a side-effect-free read
computing fresh desired-versus-actual status on every call, printed as human text or (`--json`)
the `nctl.drift.v1` envelope. It never writes a file and never pushes anything back into nintent —
there is no separate regeneration command to remember to run instead.

## `relations`

`relations` projects the same three-source state as a service-binding graph (service_relation
Phase 4, idea-A §9): "who depends on what, and is it real" for the whole cluster in one read.
It calls the exact same `fetch_and_compute_drift`/`evaluate_binding_state` primitives `drift`
does, so the two commands can never disagree about a binding's state. `--host SLUG` and
`--service NAME` filter edges on either the consumer or the provider side. The `nctl.relations.v1`
envelope contains:

- `edges`: one per consumer binding, sorted by `(consumer_node, consumer_service, binding_name)`.
  A resolved binding carries `consumer`, `binding_name`, `provider` (service/placement/node/
  endpoint/URL), `state` (the same five-state vocabulary as a binding's drift evidence: `unknown`/
  `unbound`/`misbound`/`unreachable`/`satisfied`), `gap_codes`, and `evidence` (configured
  endpoint, reachability, `age_hours`/`stale_after_hours`). A binding whose desired resolution
  itself failed (ambiguous provider, cycle, undeclared name, ...) still gets an edge — unlike
  `drift`, which folds that case into node-local production-composition drift instead — with
  `provider: null`, `state: null`, and the resolver's `error_code` as its sole `gap_codes` entry;
- `unreferenced`: services with at least one active placement but zero inbound bindings, sorted
  alphabetically. Informational only — never a deletion recommendation;
- `summary`: edge counts per state (`resolution_error` counted as its own bucket);
- `generated_at`.

Nothing here is persisted or cached: every invocation recomputes from current desired + actual
state, same as `drift`.

## Status legend

`nctl drift` targets use one status vocabulary:

| status | meaning |
|---|---|
| `converged` | no error-severity diffs |
| `converging` | diffs exist, but a newer `apply`/`reconcile` operation targets this node than its latest actual observation — change is in flight |
| `drifting` | an error-severity diff exists and nothing in flight explains it |
| `unknown` | required actual data is missing, stale, or never linked — nctl cannot see this target, which is different from it having drifted |

## Agent targets and liveness

`kind="agent"` targets (agent_intent p1) carry two independent things, and the split is
deliberate:

- **Registration gaps are drift.** `agent_zulip_account_missing`,
  `agent_zulip_account_deactivated`, `agent_zulip_channel_unsubscribed`, and
  `agent_plane_membership_missing` are error-severity: the realm does not match what was
  declared. `agent_zulip_identity_undeclared` / `agent_plane_identity_undeclared` (the
  desired row has no id to match on) and `agent_registration_unobserved` (nothing has been
  collected yet) are warnings — a hole in the declaration or in the observation, not a
  disagreement.
- **Liveness is not drift.** Every agent target also carries one info-severity
  `agent_liveness` diff whose evidence holds `liveness_class`: `polling` (its status file
  was written within three long-poll windows), `stale`, or `unobserved` with a reason.
  Info diffs never change a target's status, so a stopped listener never makes an agent
  `drifting`, and no reconciler maps this code. A listener may be restarting, mid-deploy,
  or deliberately stopped; p1 refuses to call that drift until there is false-positive data
  saying it deserves to be one.

Refresh the underlying observations with `nctl agents observe` (registration) and
`nctl reconcile <host> --refresh-observation --yes` (the node-side status file).

For a specific bounded operation's outcome, use its `result.json` (embedded in `nctl reconcile`
output, or read later via `nctl ops show`) rather than re-deriving it from a separately cached
status. For historical operations, `nctl ops list`/`nctl ops show` read past and running
operations directly from the on-disk event-log directory — they are operation history, not a
live convergence cache, so always cross-check a historical result against a fresh `nctl drift` if
the current state matters. See [ops-upload-braindump.md](ops-upload-braindump.md) for `ops`.
