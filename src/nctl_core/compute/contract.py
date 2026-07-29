"""Fixture-bound, pure compute-intent contract rules."""

from __future__ import annotations

import ipaddress
import re
from typing import TYPE_CHECKING, Any

from .model import DesiredComputeInstance, DesiredComputePlatform

if TYPE_CHECKING:
    from nctl_core.sources.desired import DesiredEndpoint


def _lower(value: Any) -> Any:
    return value.lower() if isinstance(value, str) else value


# ---------------------------------------------------------------------------
# nintent owns these semantics. nctl deliberately has no runtime dependency on
# that separately deployed package; this read-time safety code is bound to
# tests/fixtures/compute_conformance.json. Changing nctl without the owner
# fails tests/test_compute_conformance.py; changing the owner without a fresh
# fixture fails the superproject compute-conformance gate.
# ---------------------------------------------------------------------------

PROVIDER_TYPE_CHOICES = ("proxmox",)
INSTANCE_KIND_CHOICES = ("container", "virtual_machine")
POWER_STATE_CHOICES = ("running", "stopped")
COMPUTE_LIFECYCLE_CHOICES = ("planned", "approved", "active", "deprecated", "retired")
SOURCE_CHOICES = ("derived", "override")
CONFIG_SCHEMA_VERSION_V1 = "v1"

VCPUS_MIN, VCPUS_MAX = 1, 8192
MEMORY_MB_MIN, MEMORY_MB_MAX = 16, 2147483647
ROOT_DISK_GB_MIN, ROOT_DISK_GB_MAX = 1, 2147483647
VMID_MIN, VMID_MAX = 100, 999999999

PROVENANCE_INSTANCE_OVERRIDE = "instance_override"
PROVENANCE_PLATFORM_DEFAULT = "platform_default"
PROVENANCE_UNRESOLVED = "unresolved"
PROVENANCE_INTENT = "intent"

_PLATFORM_CONFIG_KEYS = {"cluster_name", "default_storage", "default_bridge"}
_INSTANCE_CONFIG_KEYS = {"vmid", "template", "storage", "bridge", "unprivileged"}

_MAC_COLON_RE = re.compile(r"^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")
_MAC_HYPHEN_RE = re.compile(r"^([0-9a-fA-F]{2}-){5}[0-9a-fA-F]{2}$")

COMPUTE_PRIMARY_ENDPOINT_MISSING = "compute_primary_endpoint_missing"
COMPUTE_PRIMARY_ENDPOINT_AMBIGUOUS = "compute_primary_endpoint_ambiguous"


class ComputeContractError(ValueError):
    """A stable, machine-readable compute-intent contract violation.

    Caught by the snapshot builder and converted into a `DesiredSourceIssue`
    -- never raised out of `fetch_desired_snapshot()`.
    """

    def __init__(self, code: str, message: str, *, path: str | None = None):
        self.code = code
        self.path = path
        prefix = f"{path}: " if path else ""
        super().__init__(f"{code}: {prefix}{message}")


def validate_provider_type(value: Any, *, path: str = "provider_type") -> str:
    if value not in PROVIDER_TYPE_CHOICES:
        raise ComputeContractError(
            "invalid_provider_type", f"only {', '.join(PROVIDER_TYPE_CHOICES)!r} is accepted", path=path
        )
    return value


def validate_compute_lifecycle(value: Any, *, path: str = "lifecycle") -> str:
    if value not in COMPUTE_LIFECYCLE_CHOICES:
        raise ComputeContractError(
            "invalid_lifecycle", f"must be one of {', '.join(COMPUTE_LIFECYCLE_CHOICES)}", path=path
        )
    return value


def validate_instance_kind(value: Any, *, path: str = "instance_kind") -> str:
    if value not in INSTANCE_KIND_CHOICES:
        raise ComputeContractError(
            "invalid_instance_kind", f"must be one of {', '.join(INSTANCE_KIND_CHOICES)}", path=path
        )
    return value


def validate_power_state(value: Any, *, path: str = "desired_power_state") -> str:
    if value not in POWER_STATE_CHOICES:
        raise ComputeContractError(
            "invalid_power_state", f"must be one of {', '.join(POWER_STATE_CHOICES)}", path=path
        )
    return value


def validate_link_source(value: Any, *, path: str) -> str:
    if value not in SOURCE_CHOICES:
        raise ComputeContractError("invalid_source", f"must be one of {', '.join(SOURCE_CHOICES)}", path=path)
    return value


def _validate_link_source_xnor(link: Any, source: Any, *, path: str) -> str | None:
    """Validate a nullable actual-link/source pair and return the lowered source.

    `source` must be set exactly when `link` is set (non-null nested `{ id }`
    dict). Used for `realized_cluster(+_source)`/`realized_vm(+_source)`.
    """
    lowered = _lower(source)
    if bool(link) != bool(lowered):
        raise ComputeContractError(
            "link_source_mismatch", f"{path} must be set exactly when the link is set", path=path
        )
    if lowered is not None:
        validate_link_source(lowered, path=path)
    return lowered


def validate_config_schema_version(value: Any, *, path: str = "config_schema_version") -> str:
    """Normalize an omitted (`None`) schema version to `v1`; reject any other explicit value."""
    if value is None:
        return CONFIG_SCHEMA_VERSION_V1
    if value != CONFIG_SCHEMA_VERSION_V1:
        raise ComputeContractError(
            "invalid_config_schema_version", f"only {CONFIG_SCHEMA_VERSION_V1!r} is accepted", path=path
        )
    return value


def _require_json_object(value: Any, path: str) -> dict:
    if not isinstance(value, dict) or isinstance(value, bool):
        raise ComputeContractError("invalid_config_type", "config must be a JSON object", path=path)
    return value


def _require_non_empty_string(value: Any, path: str, *, max_length: int) -> str:
    if isinstance(value, bool) or not isinstance(value, str):
        raise ComputeContractError("invalid_config_value", "value must be a string", path=path)
    stripped = value.strip()
    if not stripped:
        raise ComputeContractError("invalid_config_value", "value must be non-empty", path=path)
    if len(stripped) > max_length:
        raise ComputeContractError("invalid_config_value", f"value exceeds max length {max_length}", path=path)
    return stripped


def validate_platform_config(value: Any, *, path: str = "config") -> dict:
    """Validate and normalize a `DesiredComputePlatform` v1 config object.

    All three keys are optional. Unknown keys and wrong scalar types fail.
    """
    obj = _require_json_object(value if value is not None else {}, path)
    unknown = set(obj) - _PLATFORM_CONFIG_KEYS
    if unknown:
        raise ComputeContractError("unknown_config_key", f"unknown keys: {', '.join(sorted(unknown))}", path=path)
    result: dict[str, str] = {}
    for key in ("cluster_name", "default_storage", "default_bridge"):
        if key in obj:
            result[key] = _require_non_empty_string(obj[key], f"{path}.{key}", max_length=255)
    return result


def validate_instance_config(value: Any, *, instance_kind: str, path: str = "config") -> dict:
    """Validate and normalize a `DesiredComputeInstance` v1 config object.

    `instance_kind` gates the `unprivileged` key: required boolean for
    `container`, forbidden for `virtual_machine`.
    """
    obj = _require_json_object(value if value is not None else {}, path)
    unknown = set(obj) - _INSTANCE_CONFIG_KEYS
    if unknown:
        raise ComputeContractError("unknown_config_key", f"unknown keys: {', '.join(sorted(unknown))}", path=path)

    result: dict[str, Any] = {}

    if "template" not in obj:
        raise ComputeContractError("missing_config_value", "template is required", path=f"{path}.template")
    result["template"] = _require_non_empty_string(obj["template"], f"{path}.template", max_length=512)

    if "vmid" in obj:
        result["vmid"] = validate_vmid(obj["vmid"], path=f"{path}.vmid")

    for key in ("storage", "bridge"):
        if key in obj:
            result[key] = _require_non_empty_string(obj[key], f"{path}.{key}", max_length=255)

    is_container = instance_kind == "container"
    if is_container:
        if "unprivileged" not in obj:
            raise ComputeContractError(
                "missing_config_value", "unprivileged is required for container", path=f"{path}.unprivileged"
            )
        if not isinstance(obj["unprivileged"], bool):
            raise ComputeContractError(
                "invalid_config_value", "unprivileged must be a boolean", path=f"{path}.unprivileged"
            )
        result["unprivileged"] = obj["unprivileged"]
    elif "unprivileged" in obj:
        raise ComputeContractError(
            "invalid_config_key", "unprivileged is forbidden for virtual_machine", path=f"{path}.unprivileged"
        )

    return result


def validate_vmid(value: Any, *, path: str = "vmid") -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ComputeContractError("invalid_vmid", "vmid must be an integer", path=path)
    if not (VMID_MIN <= value <= VMID_MAX):
        raise ComputeContractError("vmid_out_of_range", f"vmid must be within {VMID_MIN}..{VMID_MAX}", path=path)
    return value


def _validate_bounded_int(value: Any, *, minimum: int, maximum: int, path: str, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ComputeContractError(code, "must be an integer", path=path)
    if not (minimum <= value <= maximum):
        raise ComputeContractError(code, f"must be within {minimum}..{maximum}", path=path)
    return value


def validate_vcpus(value: Any, *, path: str = "vcpus") -> int:
    return _validate_bounded_int(value, minimum=VCPUS_MIN, maximum=VCPUS_MAX, path=path, code="vcpus_out_of_range")


def validate_memory_mb(value: Any, *, path: str = "memory_mb") -> int:
    return _validate_bounded_int(
        value, minimum=MEMORY_MB_MIN, maximum=MEMORY_MB_MAX, path=path, code="memory_mb_out_of_range"
    )


def validate_root_disk_gb(value: Any, *, path: str = "root_disk_gb") -> int:
    return _validate_bounded_int(
        value, minimum=ROOT_DISK_GB_MIN, maximum=ROOT_DISK_GB_MAX, path=path, code="root_disk_gb_out_of_range"
    )


def normalize_mac_address(value: Any) -> str | None:
    """Return a canonical lower-case colon-separated MAC, or `None`.

    Six hexadecimal octets separated consistently by `:` or `-` are
    accepted. Dotted, short, overlong, mixed-separator, non-hex, list,
    numeric, and boolean values raise `ComputeContractError`.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, str):
        raise ComputeContractError("invalid_mac_address", "MAC address must be a string")
    stripped = value.strip()
    if not stripped:
        return None
    if _MAC_COLON_RE.fullmatch(stripped):
        octets = stripped.split(":")
    elif _MAC_HYPHEN_RE.fullmatch(stripped):
        octets = stripped.split("-")
    else:
        raise ComputeContractError(
            "invalid_mac_address", "must be six hex octets separated consistently by ':' or '-'"
        )
    return ":".join(octet.lower() for octet in octets)


def effective_lifecycle(node_lifecycle: str, platform_lifecycle: str) -> str:
    """Compute effective lifecycle from a DesiredNode and its DesiredComputePlatform.

    ```text
    if node or platform is retired: retired
    elif node or platform is deprecated: deprecated
    elif node or platform is planned: planned
    elif node and platform are both active: active
    else: approved
    ```
    """
    pair = (node_lifecycle, platform_lifecycle)
    if "retired" in pair:
        return "retired"
    if "deprecated" in pair:
        return "deprecated"
    if "planned" in pair:
        return "planned"
    if node_lifecycle == "active" and platform_lifecycle == "active":
        return "active"
    return "approved"


def is_actionable_lifecycle(effective: str) -> bool:
    """`active`/`approved` require a complete static-create contract; the rest do not."""
    return effective in ("active", "approved")


def effective_value(*, instance_value: Any, platform_value: Any) -> dict[str, Any]:
    """Resolve one effective value with provenance from instance override then platform default."""
    if instance_value is not None:
        return {"value": instance_value, "provenance": PROVENANCE_INSTANCE_OVERRIDE}
    if platform_value is not None:
        return {"value": platform_value, "provenance": PROVENANCE_PLATFORM_DEFAULT}
    return {"value": None, "provenance": PROVENANCE_UNRESOLVED}


def effective_single_source_value(value: Any, *, provenance: str = PROVENANCE_INTENT) -> dict[str, Any]:
    """Resolve one effective value with no fallback source, e.g. `cluster_name`/`vmid`/`mac`."""
    if value is None:
        return {"value": None, "provenance": PROVENANCE_UNRESOLVED}
    return {"value": value, "provenance": provenance}


def endpoint_has_usable_ip(endpoint: DesiredEndpoint) -> bool:
    value = endpoint.ip_address
    if not value:
        return False
    try:
        ipaddress.ip_interface(str(value))
    except ValueError:
        return False
    return True


def static_ipv4_network(endpoint: DesiredEndpoint) -> list[str] | None:
    """Return normalized initial-LXC IPv4 CIDR and gateway, or ``None``."""
    if endpoint.ip_policy != "static":
        return None
    try:
        interface = ipaddress.ip_interface(str(endpoint.ip_address or ""))
        gateway = ipaddress.ip_address(str(endpoint.gateway_address or ""))
    except ValueError:
        return None
    if interface.version != 4 or gateway.version != 4 or gateway not in interface.network:
        return None
    if gateway in {interface.network.network_address, interface.network.broadcast_address}:
        return None
    return [str(interface), str(gateway)]


def endpoint_satisfies_compute_address_contract(endpoint: DesiredEndpoint) -> bool:
    """One primary DesiredEndpoint is create-ready when it satisfies the first Proxmox contract.

    `dhcp_reserved` requires a parseable desired IP, a non-empty DNS name,
    and `generate_dnsmasq=True`. `static` requires only a parseable desired
    IP. `external` never satisfies this contract.
    """
    if endpoint.ip_policy == "dhcp_reserved":
        return (
            endpoint_has_usable_ip(endpoint)
            and bool((endpoint.dns_name or "").strip())
            and bool(endpoint.generate_dnsmasq)
        )
    if endpoint.ip_policy == "static":
        return static_ipv4_network(endpoint) is not None
    return False


def select_compute_primary_endpoint(
    node_endpoints: list[DesiredEndpoint],
) -> tuple[DesiredEndpoint | None, str | None]:
    """Select the sole NIC-bearing primary endpoint for an active/approved compute instance.

    Returns `(endpoint, None)` on exactly one match, or `(None, code)` where
    `code` is `compute_primary_endpoint_missing` (zero candidates) or
    `compute_primary_endpoint_ambiguous` (more than one). Callers must only
    invoke this for an active/approved effective lifecycle -- planned/
    deprecated/retired drafts are exempt (plan.md Section 5.4/5.5) and must
    not call this at all.
    """
    candidates = [
        endpoint
        for endpoint in node_endpoints
        if endpoint.endpoint_type == "primary"
        and endpoint.mac_address
        and (endpoint.mdns_name or "").strip()
        and endpoint_satisfies_compute_address_contract(endpoint)
    ]
    if len(candidates) == 0:
        return None, COMPUTE_PRIMARY_ENDPOINT_MISSING
    if len(candidates) > 1:
        return None, COMPUTE_PRIMARY_ENDPOINT_AMBIGUOUS
    return candidates[0], None


def effective_compute_defaults(
    instance: DesiredComputeInstance,
    platform: DesiredComputePlatform | None,
    node_endpoints: list[DesiredEndpoint],
) -> dict[str, dict[str, Any]]:
    """Resolve `cluster_name`/`storage`/`bridge`/`vmid`/`mac` with provenance (plan.md Section 5.6).

    Nctl-side representation only -- never persisted anywhere. `storage`/
    `bridge` fall back from instance override to platform default;
    `cluster_name`/`vmid` have no fallback source; `mac` reads the owning
    node's selected `DesiredEndpoint.mac_address` (see
    `select_compute_primary_endpoint`).
    """
    instance_config = instance.config or {}
    platform_config = (platform.config or {}) if platform is not None else {}
    selected_endpoint, _code = select_compute_primary_endpoint(node_endpoints)
    return {
        "cluster_name": effective_single_source_value(platform_config.get("cluster_name")),
        "storage": effective_value(
            instance_value=instance_config.get("storage"), platform_value=platform_config.get("default_storage")
        ),
        "bridge": effective_value(
            instance_value=instance_config.get("bridge"), platform_value=platform_config.get("default_bridge")
        ),
        "vmid": effective_single_source_value(instance_config.get("vmid")),
        "mac": effective_single_source_value(selected_endpoint.mac_address if selected_endpoint else None),
    }
