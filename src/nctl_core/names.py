"""Node-name normalization for fuzzy desired/actual matching.

Ported unchanged from nintent's `nautobot_intent_catalog/names.py`: used by
`drift/evaluation.py`'s node candidate-ranking (Step 4) to compare a desired
node's name/slug/hostname against an actual Device/VM's name/hostname without
being tripped up by a trailing `.local`/`.home.arpa` suffix or letter case.
"""

from __future__ import annotations

LOCAL_IDENTITY_SUFFIXES = ("local", "home.arpa")
DEFAULT_DNS_SUFFIX = "home.arpa"
DEFAULT_MDNS_SUFFIX = "local"


def canonical_node_name(value: object) -> str:
    """Return the conservative comparison name for a desired or observed node."""
    name = str(value or "").strip().lower().rstrip(".")
    if not name:
        return ""

    for suffix in LOCAL_IDENTITY_SUFFIXES:
        suffix_marker = f".{suffix}"
        if name.endswith(suffix_marker):
            return name[: -len(suffix_marker)]

    return name


def default_dns_name(node_name: object, suffix: str = DEFAULT_DNS_SUFFIX) -> str:
    label = canonical_node_name(node_name)
    suffix = str(suffix or "").strip().lower().strip(".")
    if not label:
        return ""
    if not suffix:
        return label
    return f"{label}.{suffix}"


def default_mdns_name(node_name: object) -> str:
    label = canonical_node_name(node_name)
    if not label:
        return ""
    return f"{label}.{DEFAULT_MDNS_SUFFIX}"
