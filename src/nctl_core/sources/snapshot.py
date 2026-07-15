"""Bundles the desired/actual/observed sources into one read per command
(Phase 2 Step 1).

Every future consumer (`nctl drift`, `nctl render production`, and Phase 4's
`render dnsmasq` switch-over) takes a `SourceSnapshot` instead of fetching
GraphQL or scanning the dumps dir itself, so a command reads each source at
most once.

A dump-scan error degrades `observed` only (the affected node's status
resolves to `unknown` downstream) rather than failing the whole snapshot,
matching `nctl status`'s independent-degradation convention. A GraphQL fetch
failure is fatal to the whole snapshot: unlike a stray bad dump file, a broken
desired/actual fetch means there is no trustworthy state to compare at all,
so it propagates as `NautobotError` for the caller to turn into an envelope
error (as `build_dnsmasq_render` already does).
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel

from nctl_core.config import Config
from nctl_core.dumps import scan_dumps
from nctl_core.nautobot import NautobotClient
from nctl_core.sources.actual import ActualSnapshot, fetch_actual_snapshot
from nctl_core.sources.desired import DesiredSnapshot, fetch_desired_snapshot
from nctl_core.sources.observed import ObservedFacts, read_observed_facts


class SourceSnapshot(BaseModel):
    desired: DesiredSnapshot
    actual: ActualSnapshot
    observed: list[ObservedFacts] = []
    observed_errors: list[str] = []
    fetched_at: datetime


def build_source_snapshot(cfg: Config, client: NautobotClient) -> SourceSnapshot:
    desired = fetch_desired_snapshot(client)
    actual = fetch_actual_snapshot(client)

    dump_result = scan_dumps(cfg.inventory.resolved_dumps_dir())
    observed = [read_observed_facts(dump) for dump in dump_result.dumps]

    return SourceSnapshot(
        desired=desired,
        actual=actual,
        observed=observed,
        observed_errors=dump_result.errors,
        fetched_at=datetime.now(timezone.utc),
    )
