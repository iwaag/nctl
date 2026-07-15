"""Typed three-source fetch layer for the reconciliation engine (Phase 2 Step 1).

Desired state (`desired`), actual Nautobot state (`actual`), and observed
nodeutils dumps (`observed`) each get one pinned GraphQL query or dump scan and
one set of pydantic read-models. `snapshot.build_source_snapshot` bundles all
three so every consumer (drift, `render production`, `render dnsmasq`) reads
each source at most once per command.
"""
