# Recipe: register a new PC

The literal current path from "here is a new machine" to "converged, running under nctl
reconcile" — the intent-first flow Better Usability Phase 4 (`devdocs/big/better_usability/p4/`)
consolidated. Every mechanism step below (accepted actual types, lifecycle, DNS/mDNS names) is
derived by default; you only ever supply genuine intent, and every derivation is visible with an
explicit override control if you need one.

## 1. One-time prerequisite: an `IntentSource`

Every `DesiredNode`/`DesiredService` needs a non-null `intent_source` FK. If this is your first
node, create one manual source once (`/plugins/intent-catalog/sources/add/`):

- `slug`: `manual`
- `source_type`: `Manual`
- `enabled`: checked

Skip this step entirely if a `manual` source already exists — check
`/plugins/intent-catalog/sources/` first.

## 2. Quick Host Add

Go to `/plugins/intent-catalog/nodes/quick-add/` and fill in only genuine identity/address/
publishing choices:

- `name` / `slug`: the machine's name (slug auto-generates from name if left blank).
- `node_type`: defaults to `device` (a physical machine) — the personal-cluster default since
  Better Usability Phase 4. Change to `virtual_machine`/`container`/`service_host` only if this
  registration genuinely isn't a physical device.
- `lifecycle`: defaults to `active` (Better Usability Phase 3) — the node is live and eligible
  for production composition the moment you save it. Leave it `planned` only if you deliberately
  want to stage it before it takes effect (see `nctl lifecycle` below).
- `ip_address` / `dns_name` / `mdns_name`: whatever addressing you actually have. Quick Host
  Add's publishing defaults (`generate_dnsmasq=True`, `ip_policy=dhcp_reserved`) are a narrower,
  named policy for this "one primary bootstrap endpoint" use case — they publish the address you
  give and need one to produce dnsmasq records. Turn publishing off or pick `external`/`static`
  directly if that's not what you want.

## 3. The visible derived node type, accepted actual types, lifecycle, and DNS/mDNS names

Above the "Accepted actual types override" field, the form shows a **derived preview** computed
from your selected `node_type` (e.g. `device` → `device`). Leave the override field blank to use
that derived value — this is the common case and needs no input. Only fill it in if this specific
node genuinely accepts more than one realized-object kind (e.g. a `service_host` that might
realize as either a Nautobot Device or a VM).

The success message after saving states the effective `accepted_actual_types` value and whether
it was `derived` or `override`, so you can confirm what actually got recorded before moving on.

DNS/mDNS names default from the node's slug (`names.py`'s canonical-name rules) when left blank;
an explicit value you type is recorded as `intent`, not `derived`.

## 4. Inspect recorded/effective/application layers before mutating anything

```bash
uv run --project nctl nctl drift --host NODE
```

Read the `intent_effect_summary` INFO entry for your node — three lines: `intent` (what you
recorded), `effective` (every derived/default/override mechanism value, labeled), and
`application` (whether it's `included`, `skipped`, or `out_of_scope` in production, with a reason
for every placement that isn't simply `applied`). This is the answer to "what will actually
happen" before you run anything that touches the machine. Any `error`/`warning` diffs above it
are the things worth fixing first.

## 5. Review the bounded plan

```bash
uv run --project nctl nctl reconcile NODE
```

No `--yes` yet — this is a dry plan with zero writes. Confirm the actions it proposes
(bootstrap collection, IPAM linking, production render) match what you expect.

## 6. Apply

```bash
uv run --project nctl nctl reconcile NODE --yes
```

One bounded operation: bootstrap collection, ledger/IPAM actions, a fresh production render, and
verification, ending in a final drift check. This replaces any manual
`ansible-playbook`/`make bootstrap-inventory` sequence for this node.

## 7. Final host-scoped drift

```bash
uv run --project nctl nctl drift --host NODE
```

The remaining `intent_effect_summary` explains the mechanism that converged (or, if something is
still short of `converged`, exactly which finding is blocking and why — never a silent gap).

## Staying `planned` on purpose

If you want a node recorded but not yet live (a future secure-route entry point, or a machine
you're not ready to actuate), leave `lifecycle=planned` in step 2 or demote it afterward:

```bash
uv run --project nctl nctl lifecycle NODE planned
```

A `planned` node's recorded intent is still fully visible in `intent_effect_summary`
(`production.state: out_of_scope`, reason `node_out_of_scope` on any active placement) — nothing
about it is hidden, it just doesn't actuate until promoted (`nctl lifecycle NODE active`).

## Blank `IntentSource.ref` resolution

If an `IntentSource` used for analysis (not this manual-registration path, but relevant if you
also configure Git-backed sources) has no explicit `ref`, analysis tries the repository's
discovered default branch first, then the deduplicated fallbacks `HEAD`, `main`, `master` in that
order. An explicit `ref` always wins and is tried first.

## Next: add a service

Once the node itself is converged, see [add-a-basic-service.md](add-a-basic-service.md) to place
a service on it.
