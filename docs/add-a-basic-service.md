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
- The target `DesiredNode` (and, if you want mDNS bootstrap connectivity, a
  `DesiredEndpoint` with `mdns_name` on it) already exists. A newly created
  node is `active` by default (Better Usability Phase 3) — no lifecycle
  promotion step and no operational-config row are required before it's
  eligible for production composition; see `nctl lifecycle` in
  `nctl/README.md` only if you deliberately want to stage it as `planned`
  first.

## Steps

1. Create a `DesiredService` — one row per service, not per instance:
   - `name` / `slug`: e.g. `dnsmasq`.
   - `display_name`: human label.
   - `service_type`: `service` (or the closest fit).
   - `lifecycle`: `active` once you intend it to actually run.
   - `intent_source`: required (non-null FK) — use a `manual` IntentSource
     for hand-entered services; create one (`slug="manual"`,
     `source_type="manual"`) if none exists yet.
   - `catalog_namespace` / `catalog_metadata_name`: `default` / the service
     slug is enough for a manual entry (these plus `intent_source` are the
     row's uniqueness key).

2. Create a `DesiredServicePlacement` binding that service to the target
   node:
   - `desired_service`: the row from step 1.
   - `desired_node`: the target `DesiredNode` (e.g. `agdnsmasq`).
   - `instance_name`: unique per service (e.g. `dnsmasq`) — `(desired_service,
     instance_name)` is the uniqueness key, so a service can have multiple
     named instances across nodes.
   - `desired_state`: `active` to actuate/observe it; `disabled` to declare
     intent without provisioning yet.
   - `deployment_profile`: must match a key in `deployment_profiles.yml`
     (e.g. `dnsmasq`) — this is what derives group membership in every
     generated inventory.
   - `config_schema_version`: `"1"` unless the profile defines a newer one.
   - `config`: a JSON object of the operational knobs the profile's
     `variables` map accepts (for `dnsmasq`: `interfaces`, `enable_dhcp`,
     `local_domain`, `upstream_servers`, `listen_addresses`,
     `bind_interfaces`, `cache_size`, `dhcp_authoritative`). An empty `{}` is
     valid — the Ansible role's own defaults apply (DHCP off, listens on
     `127.0.0.1` only), which is the safe starting point; override only the
     knobs your deployment actually needs.

That's it — no code, no nintent push/rebuild. Both `nctl render hosts-intent`
(bare group, for bootstrap-time playbooks before any production inventory
exists) and `nctl render production` (full variables, resolved from
`config`/ledger) pick the placement up on their next run.

## Example: dnsmasq on agdnsmasq

Declared on the dev instance (`nautobot-server shell`, since nintent's REST
API only exposes `nodes`/`services`/`endpoints` viewsets — `IntentSource` and
`DesiredServicePlacement` have no REST endpoint yet, only Django admin/UI
forms):

```python
from nautobot_intent_catalog.models import (
    IntentSource, DesiredService, DesiredServicePlacement, DesiredNode,
)

source, _ = IntentSource.objects.get_or_create(
    slug="manual",
    defaults={"name": "Manual", "source_type": IntentSource.SOURCE_MANUAL, "enabled": True},
)

service, _ = DesiredService.objects.get_or_create(
    intent_source=source,
    catalog_namespace="default",
    catalog_metadata_name="dnsmasq",
    defaults={
        "name": "dnsmasq",
        "slug": "dnsmasq",
        "display_name": "dnsmasq",
        "service_type": DesiredService.SERVICE_TYPE_SERVICE,
        "lifecycle": DesiredService.LIFECYCLE_ACTIVE,
    },
)

node = DesiredNode.objects.get(slug="agdnsmasq")

placement, _ = DesiredServicePlacement.objects.get_or_create(
    desired_service=service,
    instance_name="dnsmasq",
    defaults={
        "desired_node": node,
        "desired_state": DesiredServicePlacement.STATE_ACTIVE,
        "deployment_profile": "dnsmasq",
        "config_schema_version": "1",
        "config": {},  # safe defaults; override interfaces/enable_dhcp/local_domain for a real network
        "assignment_source": DesiredServicePlacement.SOURCE_MANUAL,
    },
)
```

Verify with `nctl render hosts-intent --json` (or `render production`): the
`dnsmasq_server` group should appear with `agdnsmasq` as a bare member.

## Known gap (not blocking, out of scope here)

`GET /api/plugins/intent-catalog/services/` currently 500s
(`ImproperlyConfigured`) once any `DesiredService` has a non-null
`intent_source`, because its `NautobotModelSerializer` auto-generates a
hyperlinked field for `intent_source` pointing at
`intentsource-detail`, a view that was never registered
(`nautobot_intent_catalog/api/urls.py` only registers `nodes`, `services`,
`endpoints`). This does not affect `nctl` — every fetch here goes through
GraphQL (`sources/desired.py::DESIRED_QUERY`), which only serializes
explicitly requested fields and has no such hyperlink resolution step. Worth
a fix in `nintent` itself (register an `IntentSource` viewset, or drop
`intent_source` from the REST serializer's `fields`), tracked separately from
this plan.
