# Current-consumer contract policy

The root repository's coordinated breaking-change rule governs nctl. JSONL event logs, operation
artifacts, and CLI envelopes are local durable evidence read by current CLI, agent, and operator
workflows; they are not an external subscriber API.

A schema change is made in one matched-version rollout: update its writer, every current reader,
documentation, and the exact contract test together. Do not retain obsolete writers, serializers,
aliases, or versions solely for compatibility. A version label identifies the current contract;
it does not require obsolete producers to run indefinitely.

Existing on-disk operation evidence is different: `nctl ops show` must continue to inspect it.
When a historic artifact needs a removed presentation field, retain the smallest historical reader
or provide an explicit offline migration. This does not require retaining the old writer or an
obsolete dashboard schema.

## Named contracts

| Contract | Writer | Current reader | Durable reader |
|---|---|---|---|
| `EventRecord` JSONL | `nctl_core.events` | `nctl ops list` / `show` | historical operation logs |
| `nctl.drift.v1` | `nctl drift` | reconcile; AI/operator inspection | — |
| `nctl.render.dnsmasq.v3` | `nctl render dnsmasq` | reconcile; Ansible actuation | — |
| `nctl.render.hosts-intent.v1` | `nctl render hosts-intent` | inventory composition; Ansible | — |
| `nctl.render.production.v1` | `nctl render production` | inventory composition; Ansible | — |
| operations index/list/show | operations index and `nctl ops` | `nctl ops` | historical indexes and artifacts |
| `nctl.reconcile.v2` | reconcile executor | operation artifacts; `nctl ops show` | historical operation results |
| status, apply, lifecycle, SSH enrollment, and Braindump envelopes | their named command | CLI/agent caller of that command | — |

`tests/test_current_consumer_contracts.py` pins the exact current shapes for these command
envelopes. The operation-index tests own real JSONL/index write-read, corruption, restart, and
historical-dashboard-field readability. Command tests own their command-specific behavior.

## Scope

This is not a JSON-Schema registry or a promise of backwards-compatible runtime producers.
Internal module layout, CLI flags, and configuration shape are outside this policy. A future
change that requires retained evidence readability must document its minimal reader or offline
migration before removing the old representation.
