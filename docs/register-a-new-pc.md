# Recipe: register a new PC

> [!NOTE]
> **Superseded (Interface Contract Phase 3/4):** the nintent Nautobot UI is read-only. UI
> add/edit/delete forms, `sources/add/`, and Quick Host Add (`nodes/quick-add/`) have been
> deleted, not merely deprecated. Sections 1-3 below describe the current, only path: declare
> the `IntentSource`, `DesiredNode`, and `DesiredEndpoint` rows in `nauto/seed/intent_sources.yaml`
> and load them with the `Import Intent Sources` Job (`apply=false` to preview, then `apply=true`).
> Use `nctl lifecycle NODE` for lifecycle transitions.

The literal current path from "here is a new machine" to "converged, running under nctl
reconcile" — the intent-first flow Better Usability Phase 4 (`devdocs/big/better_usability/p4/`)
consolidated, now expressed as YAML instead of a UI form. Every mechanism step below (accepted
actual types, lifecycle, DNS/mDNS names) is derived by default; you only ever supply genuine
intent, and every derivation is visible with an explicit override control if you need one.

## 1. One-time prerequisite: an `IntentSource`

Every `DesiredNode`/`DesiredService` needs a non-null `intent_source` FK. If this is your first
node, add one entry under the `intent_sources` root of `nauto/seed/intent_sources.yaml`:

```yaml
intent_sources:
  - slug: manual
    source_type: manual
    enabled: true
```

Skip this step entirely if a `manual` source already exists — check the existing
`intent_sources` root, or the read-only `/plugins/intent-catalog/sources/` list page, first.

## 2. Declare the node and its endpoint in YAML

Add entries under the `desired_nodes` and `desired_endpoints` roots of the same file, filling in
only genuine identity/address/publishing choices:

- `name` / `slug`: the machine's name.
- `node_type`: `device` (a physical machine) is the personal-cluster default since Better
  Usability Phase 4. Use `virtual_machine`/`container`/`service_host` only if this registration
  genuinely isn't a physical device.
- `lifecycle`: `active` (Better Usability Phase 3) makes the node live and eligible for
  production composition as soon as the Import Job applies it. Use `planned` only if you
  deliberately want to stage it before it takes effect (see `nctl lifecycle` below).
- the endpoint's `ip_address` / `dns_name` / `mdns_name`: whatever addressing you actually have.
  `generate_dnsmasq: true` with `ip_policy: dhcp_reserved` is the narrower, named policy for a
  "one primary bootstrap endpoint" use case — it publishes the address you give and needs one to
  produce dnsmasq records. Turn publishing off or pick `external`/`static` directly if that's not
  what you want.

Run the `Import Intent Sources` Job with `apply=false` first and review the proposed
create/update actions in its artifact before applying; run again with `apply=true` once the
preview matches what you intended.

## 3. The derived node type, accepted actual types, lifecycle, and DNS/mDNS names

`accepted_actual_types` is derived from `node_type` (e.g. `device` -> `device`) when omitted from
the YAML — this is the common case and needs no input. Set it explicitly only if this specific
node genuinely accepts more than one realized-object kind (e.g. a `service_host` that might
realize as either a Nautobot Device or a VM).

The Import Job's preview/apply artifact states the effective `accepted_actual_types` value and
whether it was `derived` or an explicit override, so you can confirm what actually got recorded
before moving on. The read-only detail page for the node
(`/plugins/intent-catalog/desired-nodes/<id>/`) shows the same recorded/effective values after
apply.

DNS/mDNS names default from the node's slug (`names.py`'s canonical-name rules) when omitted from
the endpoint's YAML; an explicit value you supply is recorded as `intent`, not `derived`.

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
