# Recipe: add a basic service

This is the reference sequence for declaring that a service should run on some
node — "dnsmasq is data, not schema" (see
`devdocs/small/basic_service/plan.md`). It requires **no nintent schema
change**: `DesiredService` + `DesiredServicePlacement` are the generic
placement mechanism every service uses, and group membership in every
generated inventory (`render hosts-intent`, `render production`) is always
derived from active placements + `ansible_agdev/vars/deployment_profiles.yml`
— never hand-maintained.

## Prerequisites

- The service's `deployment_profile` already exists in
  `ansible_agdev/vars/deployment_profiles.yml`, with a `group` name and a
  `variables` map (this is how a placement's `config` becomes Ansible
  variables — see `production/composer.py::map_placement_config`).
- The target `DesiredNode` already exists and is converged — see
  [register-a-new-pc.md](register-a-new-pc.md) if it doesn't yet. A newly
  created node is `active` by default (Better Usability Phase 3) — no
  lifecycle promotion step and no operational-config row are required before
  it's eligible for production composition.

## Steps (Nautobot UI — no nautobot-server shell)

Both models have full CRUD UI under `/plugins/intent-catalog/` (and `nodes`/
`services`/`endpoints` also have a REST API, per `nctl/README_DEV.md`;
`placements` is UI-only today).

1. **Create a `DesiredService`** at `/plugins/intent-catalog/services/add/` —
   one row per service, not per instance:
   - `name` / `slug`: e.g. `dnsmasq`.
   - `display_name`: human label.
   - `service_type`: `service` (or the closest fit).
   - `lifecycle`: **set this to `active` explicitly** once you intend it to
     actually run. Unlike `DesiredNode.lifecycle`, `DesiredService.lifecycle`
     deliberately kept its `proposed` default in Better Usability Phase 3 —
     nothing promotes a service to `active` for you, so this is genuine
     intent you must state, not an oversight.
   - `intent_source`: required (non-null FK) — use a `manual` IntentSource
     for hand-entered services; create one at
     `/plugins/intent-catalog/sources/add/` (`slug="manual"`,
     `source_type="manual"`) if none exists yet.
   - `catalog_namespace` / `catalog_metadata_name`: `default` / the service
     slug is enough for a manual entry (these plus `intent_source` are the
     row's uniqueness key).
   - `requirements`: leave `{}` unless you have genuine operator-declared
     requirements to record — this field is never populated by analysis
     (Better Usability Phase 4 keeps `analysis_provenance` strictly separate
     from operator intent, so a re-analysis run can never overwrite what you
     put here).

2. **Create a `DesiredServicePlacement`** at
   `/plugins/intent-catalog/placements/add/`, binding that service to the
   target node:
   - `desired_service`: the row from step 1.
   - `desired_node`: the target `DesiredNode` (e.g. `agdnsmasq`).
   - `instance_name`: unique per service (e.g. `dnsmasq`) — `(desired_service,
     instance_name)` is the uniqueness key, so a service can have multiple
     named instances across nodes.
   - `desired_state`: `active` to actuate/observe it; `disabled` to declare
     intent without provisioning yet.
   - `deployment_profile`: must match a key in `deployment_profiles.yml`
     (e.g. `dnsmasq`) — this, together with `config`, is genuine placement
     intent, not a derived value: which profile a placement uses and which
     knobs it sets are choices only you can make.
   - `config_schema_version`: `"1"` unless the profile defines a newer one.
   - `config`: a JSON object of the operational knobs the profile's
     `variables` map accepts (for `dnsmasq`: `interfaces`, `enable_dhcp`,
     `local_domain`, `upstream_servers`, `listen_addresses`,
     `bind_interfaces`, `cache_size`, `dhcp_authoritative`). An empty `{}` is
     valid — the Ansible role's own defaults apply (DHCP off, listens on
     `127.0.0.1` only), which is the safe starting point; override only the
     knobs your deployment actually needs. A non-default `config` is genuine
     placement intent too — it is never inferred.

## Verify with drift before touching anything

```bash
uv run --project nctl nctl drift --host NODE
```

Check the node's `intent_effect_summary` `application` line: your new
placement should show `applied` if the node is already `included` in
production, or a specific reason (e.g. `node_out_of_scope`,
`node_skipped`) if it isn't yet. This is the same "recorded but not applied"
visibility the plan requires — a placement that doesn't apply always says
why, never silently.

## Dry-run, then apply

```bash
uv run --project nctl nctl reconcile NODE
uv run --project nctl nctl reconcile NODE --yes
```

The first call is a bounded plan with zero writes; review it, then re-run
with `--yes` to actuate (regenerates the production inventory, runs the
matching Ansible play, and re-verifies).

## Confirm the placement effect

```bash
uv run --project nctl nctl drift --host NODE
```

The remaining `intent_effect_summary` should show `effect: applied` for this
placement once converged.

## The dnsmasq self-bootstrap exception

A brand-new dnsmasq node has no production inventory entry yet (nodeutils
hasn't observed it, so it can't enter production composition). This is the
**one** documented exception to "service actuation normally uses the
production inventory nctl regenerates": `nctl apply dnsmasq --inventory
PATH` can target the bootstrap `hosts_intent.yml` inventory for exactly this
one-time window. See
[`nctl/README.md`](../README.md#usage)'s "bootstrap escape hatch" section for
the full sequence — it is not part of the ordinary service path above and
`nctl reconcile` never does this automatically.
