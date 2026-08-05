# `apply dnsmasq`

See [`../README.md`](../README.md#usage) for the full command list, and
[`reconcile.md`](reconcile.md) for the routine `reconcile --yes` path that supersedes most direct
`apply dnsmasq` use.

`apply dnsmasq` without `--yes` is a pure plan: it renders an operation-specific artifact and
resolves the `dnsmasq_server` target group from the generated YAML inventory. It does not invoke
SSH, `ansible-inventory`, or Ansible check mode. Review that plan, then use `--yes` for SSH
preflight, daemon setup, and deployment. The configured inventory must resolve at least one host
in `dnsmasq_server`; an existing inventory file with an empty or missing group is rejected instead
of succeeding as a no-op. Direct `apply dnsmasq` always targets the whole `dnsmasq_server`
group; a `reconcile`-driven `dnsmasq_config` action instead scans, deploys, and re-observes only its
exact planned host set (`fix_sshkey4` Step 3), so a host-scoped reconcile can never actuate a
sibling placement it never scanned. The deployed destination path is resolved exactly once from
validated `deployment_profile_reconciliation` metadata and passed to the playbook as a structured
extra-vars payload -- it is never a literal the playbook constructs itself. Content drift also
checks the observed managed-file path and digest algorithm, not only the digest
(`service_config_observation_mismatch`): a digest match at the wrong reported path plans a fresh
observation rather than a blind deploy.

## Bootstrap escape hatch

`apply dnsmasq --inventory PATH` overrides the configured `[ansible].inventory` for that one run —
the bootstrap escape hatch for a freshly registered node that has no production inventory entry
yet. No silent fallback: omit `--inventory` and it uses the configured production inventory as
always; `reconcile` never passes an override, it always actuates against the production inventory
it regenerates itself. Bootstrap sequence for a brand-new dnsmasq node (see
[`add-a-basic-service.md`](add-a-basic-service.md) for declaring the placement first):

```bash
uv run nctl render hosts-intent --out ansible_agdev/inventories/generated
uv run nctl apply dnsmasq --inventory ansible_agdev/inventories/generated/hosts_intent.yml
uv run nctl apply dnsmasq --inventory ansible_agdev/inventories/generated/hosts_intent.yml --yes
```

Once nodeutils collection + ingest have run against the new host, `nctl render production` and
subsequent `nctl apply dnsmasq`/`nctl reconcile` runs use the regenerated production inventory as
usual — the override is only for the one-time bootstrap window before it exists.

## Routine use

`apply dnsmasq --yes` remains useful for a reviewed, direct deployment. Routine DNS, DHCP
reservation, and DHCP-range intent changes should use `nctl reconcile --yes`: a running daemon
with a mismatching managed-file digest is real `service_config_mismatch` drift and is re-observed
after deployment. The content contract covers only nctl's
`/etc/dnsmasq.d/nintent-records.conf`, not every dnsmasq package default or `ansible.conf` setting.
