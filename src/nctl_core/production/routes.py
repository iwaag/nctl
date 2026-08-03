"""Pure production connection-route resolution rules.

This module owns the route priority and host-variable connection policy.  It
is deliberately separate from inventory schema validation: route policy also
has consumers that validate an already-rendered inventory.
"""

from __future__ import annotations

import ipaddress
from typing import Any, Mapping

from .contract import ContractError


def select_local_route(*, local_ip: str | None, local_dns_hostname: str | None,
                       mdns_hostname: str | None, inventory_hostname: str) -> str:
    """Pick the local-path route in stable priority order."""
    candidates = (local_ip, local_dns_hostname, mdns_hostname, inventory_hostname)
    return next(value for value in candidates if isinstance(value, str) and value.strip())


def resolve_connection_variables(*, inventory_hostname: str,
                                 connection_path: str, actual_local_ip: str | None = None,
                                 local_endpoint: Mapping[str, Any] | None = None,
                                 tailscale_endpoint: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Resolve desired/actual connection variables allowed by production schema 3.0."""
    variables: dict[str, Any] = {"connection_path": connection_path}
    local_endpoint = local_endpoint or {}
    tailscale_endpoint = tailscale_endpoint or {}
    if actual_local_ip:
        variables["local_ip"] = _normalize_ip(actual_local_ip, "actual_local_ip")
    if _nonempty(local_endpoint.get("dns_name")):
        variables["local_dns_hostname"] = local_endpoint["dns_name"].strip()
    if _nonempty(local_endpoint.get("mdns_name")):
        variables["mdns_hostname"] = local_endpoint["mdns_name"].strip()
    if tailscale_endpoint.get("ip_address"):
        variables["tailscale_ip"] = _normalize_ip(tailscale_endpoint["ip_address"], "tailscale_endpoint.ip_address")
    if connection_path == "local":
        variables["ansible_host"] = select_local_route(
            local_ip=variables.get("local_ip"), local_dns_hostname=variables.get("local_dns_hostname"),
            mdns_hostname=variables.get("mdns_hostname"), inventory_hostname=inventory_hostname)
    elif connection_path == "tailscale":
        if "tailscale_ip" not in variables:
            raise ContractError("unresolved_connection_path", "tailscale path requires a usable tailscale endpoint")
        variables["ansible_host"] = variables["tailscale_ip"]
    else:
        raise ContractError("unresolved_connection_path", f"unsupported connection path {connection_path!r}")
    return variables


def _normalize_ip(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise ContractError("invalid_ip_address", "must be a string", path=path)
    try:
        return str(ipaddress.ip_interface(value).ip)
    except ValueError as exc:
        raise ContractError("invalid_ip_address", "must be an IP address or interface", path=path) from exc


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())
