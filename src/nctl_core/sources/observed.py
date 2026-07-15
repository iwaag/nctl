"""Typed accessor over nodeutils dump `facts` (Phase 2 Step 1).

`nctl_core.dumps` deliberately left `facts`/`self_reported` raw ("Phase 2 owns
their typing" — see `dumps.py`'s module docstring); this is that typing. It
reads the same `facts.system` / `facts.network.primary_mac_address` /
`facts.network.primary_ip_address` / `facts.network.primary_interface.name`
shape that nauto's `ingest_nodeutils_inventory.py` (`build_custom_fields`)
reads before writing the actual-fact custom fields `sources/actual.py` reads
back out. The two must stay in sync: a `nctl drift` comparator needs to
compare "what the dump says right now" against "what Nautobot ingested last".
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from nctl_core.dumps import NodeDump


class ObservedFacts(BaseModel):
    hostname: str
    serial_number: str | None = None
    collected_at: datetime
    system: str | None = None
    primary_mac_address: str | None = None
    primary_ip_address: str | None = None
    primary_interface: str | None = None


def read_observed_facts(dump: NodeDump) -> ObservedFacts:
    facts = dump.facts
    network = _mapping(facts.get("network"))
    primary_interface = _mapping(network.get("primary_interface"))
    return ObservedFacts(
        hostname=dump.identity.hostname,
        serial_number=getattr(dump.identity, "serial_number", None),
        collected_at=dump.collected_at,
        system=facts.get("system"),
        primary_mac_address=network.get("primary_mac_address"),
        primary_ip_address=network.get("primary_ip_address"),
        primary_interface=primary_interface.get("name"),
    )


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
