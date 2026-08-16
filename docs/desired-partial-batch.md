# Hand-writing a partial desired-state batch

`nctl desired apply -f` accepts one batch document; `op: upsert` is a **partial upsert**, so a
hand-written document only needs the fields you are changing plus the target's identity key.
`op: delete` removes a row (see below).
This page is the minimal template for that case — for full-object examples see
[`register-a-new-pc.md`](register-a-new-pc.md), [`add-a-basic-service.md`](add-a-basic-service.md)
and [`add-and-retire-proxmox-lxc.md`](add-and-retire-proxmox-lxc.md).

## Minimal template

```yaml
# dry_run is optional: the CLI always sets it from --yes (absent → preview).
operations:
  - op: upsert
    kind: desired_endpoint
    key: {desired_node: agdnsmasq, name: primary, endpoint_type: primary}
    values:
      ip_address: 192.168.50.53/24     # CIDR notation
      gateway_address: 192.168.50.1
```

```bash
uv run --project nctl nctl desired apply -f batch.yaml        # preview, zero writes
uv run --project nctl nctl desired apply -f batch.yaml --yes  # commit
```

## Deleting rows

`op: delete` uses the same document shape, and **`values: {}` must be present and empty** —
every operation must contain exactly the four members `op`, `kind`, `key`, `values`, so omitting
`values` fails validation, and a non-empty `values` on a delete is refused. Watch out: over the
CLI a validation failure surfaces as a bare `HTTP 400`; run with `--json` to see the reason
(e.g. `operations[0] must contain op, kind, key, values`).

```yaml
operations:
  - op: delete
    kind: desired_service_binding
    key:
      consumer_placement: {desired_service: node-agent, instance_name: node-agent-aghub}
      binding_name: llm_provider
    values: {}
```

A service, its placements, and their bindings can go in one batch in any order: deletes are
applied leaf-first automatically (reverse dependency order), and a delete whose dependents are
not also being deleted plans as `conflict` rather than leaving an orphan.

Note that `nctl prune` is **not** the follow-up for service removals — it is guest-scoped
(`nctl prune GUEST` collects a fully-retired guest's Desired and Actual records; see
[`add-and-retire-proxmox-lxc.md`](add-and-retire-proxmox-lxc.md)). A delete batch removes the
rows directly; there is nothing left to prune.

## Identity keys per kind

`key` must contain exactly these members, each non-empty (order does not matter). The source of
truth is `_KEYS` in `nintent/nautobot_intent_catalog/batch.py`; `_FIELDS` beside it lists the
writable `values` fields per kind.

| kind | identity key members |
|---|---|
| `desired_node` | `slug` |
| `desired_ip_range` | `slug` |
| `desired_endpoint` | `desired_node`, `name`, `endpoint_type` |
| `desired_compute_platform` | `slug` |
| `desired_compute_instance` | `desired_node` |
| `desired_service` | `slug` |
| `desired_service_placement` | `desired_service`, `instance_name` |
| `desired_service_binding` | `consumer_placement`, `binding_name` |
| `desired_node_operational_override` | `desired_node` |
| `desired_workspace` | `slug` |
| `desired_agent` | `slug` |

`consumer_placement` is itself a dict identity:
`{desired_service: <slug>, instance_name: <name>}`. So is a `desired_agent`'s
`desired_service_placement` reference.

## Getting a known-good starting point

`nctl desired export` emits the complete current desired state in this exact document shape, so
copying one operation out of an export and trimming `values` down to the fields you are changing
is always a valid starting point.
