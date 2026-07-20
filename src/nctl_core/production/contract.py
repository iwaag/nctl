"""Pure production-inventory contract helpers, ported from nintent's
`production_inventory_contract.py` (Phase 2 Step 2).

Ported unchanged: `PRODUCTION_INVENTORY_SCHEMA_VERSION`, `ContractError`,
`canonical_json`/`canonical_json_digest`, `validate_deployment_profiles`,
`map_placement_config`, `validate_endpoint_ownership`,
`actual_state_problem`,
`resolve_connection_variables`, `merge_host_variables`,
`validate_production_inventory_document`, `validate_production_report`, and
their private helpers.

**Not ported** (see `profiles.py`'s docstring): `parse_profile_job_input` and
`_raise_invalid_constant` (the Job-input byte-contract transport, replaced by
reading `vars/deployment_profiles.yml` directly) and
`validate_desired_service_reference` / `validate_endpoint_reference` /
`require_unique_reference` (the YAML-catalog-import reference validators —
used only by nintent's `loaders.py`/`Import Intent Sources` Job, which stay
ledger-side and are not part of production composition).
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

PRODUCTION_INVENTORY_SCHEMA_VERSION = "2.0"
PRODUCTION_PROFILE_CONTRACT_VERSION = "1"
ACTUAL_MAX_AGE_HOURS = 72

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")
_PROFILE_KEYS = {"group", "config_schema_version", "variables"}
_VARIABLE_KEYS = {"ansible_variable", "type", "required", "items"}
_JSON_TYPES = {"string", "integer", "number", "boolean", "list"}
_INVENTORY_METADATA_KEYS = {
    "nintent_inventory_schema_version",
    "nintent_generation_id",
    "nintent_generated_at",
    "nintent_report_path",
    "nintent_deployment_profile_digest",
}
_BASE_HOST_VARIABLES = {
    "host_os",
    "local_ip",
    "mac_address",
    "network_interface",
    "connection_path",
    "local_dns_hostname",
    "mdns_hostname",
    "tailscale_ip",
    "ansible_port",
    "power_control",
    "is_laptop",
    "nintent_desired_node_id",
    "nautobot_device_id",
    "nintent_active_placement_ids",
}
_REPORT_KEYS = {
    "schema_version",
    "generation_id",
    "generated_at",
    "report_path",
    "deployment_profile_digest",
    "summary",
    "hosts",
    "skipped",
    "drift",
    "errors",
}
_REPORT_SUMMARY_KEYS = {
    "eligible",
    "included",
    "skipped",
    "placements",
    "active_placements",
    "inactive_placements",
}
_REPORT_HOST_KEYS = {
    "inventory_hostname",
    "desired_node_id",
    "host_os",
    "connection_path",
    "actual_state_policy",
    "nautobot_device_id",
    "active_placement_ids",
    "operational_values",
}
_OPERATIONAL_VALUE_KEYS = {
    "actual_state_policy",
    "host_os",
    "connection_path",
    "connection_endpoint",
    "connection_address",
    "ansible_port",
    "power_control",
    "is_laptop",
}
_VALUE_RECORD_KEYS = {"value", "source", "source_reference", "override_won"}


class ContractError(ValueError):
    """A stable, machine-readable production contract violation."""

    def __init__(self, code: str, message: str, *, path: str | None = None):
        self.code = code
        self.path = path
        prefix = f"{path}: " if path else ""
        super().__init__(f"{code}: {prefix}{message}")


def canonical_json(value: Any) -> str:
    """Serialize a JSON value using the production Job-input byte contract."""

    _require_string_mapping_keys(value)
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ContractError("invalid_profile_json", str(exc)) from exc


def canonical_json_digest(value: Any) -> str:
    """Return the SHA-256 digest of canonical UTF-8 JSON bytes."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def validate_deployment_profiles(value: Any) -> dict[str, Any]:
    """Validate and return the strict ``deployment_profiles`` mapping."""

    if not isinstance(value, dict):
        raise ContractError("invalid_profile_map", "deployment_profiles must be an object")
    validated: dict[str, Any] = {}
    groups: dict[str, str] = {}
    for profile_name in sorted(value):
        path = f"deployment_profiles.{profile_name}"
        _require_slug(profile_name, path)
        profile = value[profile_name]
        if not isinstance(profile, dict):
            raise ContractError("invalid_profile", "profile must be an object", path=path)
        _require_exact_keys(profile, _PROFILE_KEYS, path)
        group = profile["group"]
        _require_slug(group, f"{path}.group")
        if group in groups:
            raise ContractError(
                "duplicate_profile_group",
                f"group is already owned by profile {groups[group]!r}",
                path=f"{path}.group",
            )
        groups[group] = profile_name
        schema_version = profile["config_schema_version"]
        if schema_version != PRODUCTION_PROFILE_CONTRACT_VERSION:
            raise ContractError(
                "unsupported_profile_schema",
                f"only config schema {PRODUCTION_PROFILE_CONTRACT_VERSION!r} is supported",
                path=f"{path}.config_schema_version",
            )
        variables = profile["variables"]
        if not isinstance(variables, dict):
            raise ContractError("invalid_profile_variables", "variables must be an object", path=f"{path}.variables")
        ansible_names: dict[str, str] = {}
        for config_key in sorted(variables):
            variable_path = f"{path}.variables.{config_key}"
            _require_slug(config_key, variable_path)
            definition = variables[config_key]
            if not isinstance(definition, dict):
                raise ContractError("invalid_profile_variable", "definition must be an object", path=variable_path)
            allowed_keys = set(_VARIABLE_KEYS)
            required_keys = {"ansible_variable", "type", "required"}
            unknown = set(definition) - allowed_keys
            missing = required_keys - set(definition)
            if unknown or missing:
                _raise_key_error(unknown, missing, variable_path)
            ansible_name = definition["ansible_variable"]
            if not isinstance(ansible_name, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", ansible_name):
                raise ContractError("invalid_ansible_variable", "must be a lowercase Ansible variable name", path=f"{variable_path}.ansible_variable")
            if ansible_name in ansible_names:
                raise ContractError(
                    "duplicate_variable_assignment",
                    f"also assigned by config key {ansible_names[ansible_name]!r}",
                    path=f"{variable_path}.ansible_variable",
                )
            ansible_names[ansible_name] = config_key
            value_type = definition["type"]
            if value_type not in _JSON_TYPES:
                raise ContractError("unsupported_profile_type", f"unsupported type {value_type!r}", path=f"{variable_path}.type")
            if not isinstance(definition["required"], bool):
                raise ContractError("invalid_profile_required", "required must be boolean", path=f"{variable_path}.required")
            if value_type == "list":
                item_type = definition.get("items")
                if item_type not in _JSON_TYPES - {"list"}:
                    raise ContractError("invalid_profile_item_type", "list items must be a supported scalar type", path=f"{variable_path}.items")
            elif "items" in definition:
                raise ContractError("unexpected_profile_items", "items is allowed only for list variables", path=f"{variable_path}.items")
        validated[profile_name] = profile
    return validated


def map_placement_config(
    profile_name: str,
    config_schema_version: str,
    config: Any,
    profiles: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate placement config and map it to audited Ansible variables."""

    validated_profiles = validate_deployment_profiles(dict(profiles))
    if profile_name not in validated_profiles:
        raise ContractError("unknown_profile", f"unknown deployment profile {profile_name!r}")
    profile = validated_profiles[profile_name]
    if config_schema_version != profile["config_schema_version"]:
        raise ContractError(
            "unsupported_config_schema",
            f"profile {profile_name!r} requires schema {profile['config_schema_version']!r}",
        )
    if not isinstance(config, dict):
        raise ContractError("invalid_placement_config", "placement config must be an object")
    definitions = profile["variables"]
    unknown = sorted(set(config) - set(definitions))
    if unknown:
        raise ContractError("unknown_config_key", f"unknown keys: {', '.join(unknown)}")
    missing = sorted(
        key for key, definition in definitions.items() if definition["required"] and key not in config
    )
    if missing:
        raise ContractError("missing_required_config", f"missing keys: {', '.join(missing)}")
    mapped: dict[str, Any] = {}
    for key in sorted(config):
        definition = definitions[key]
        if not _matches_json_type(config[key], definition["type"], definition.get("items")):
            raise ContractError(
                "invalid_profile_value_type",
                f"config key {key!r} must be {definition['type']}",
                path=f"config.{key}",
            )
        mapped[definition["ansible_variable"]] = config[key]
    return mapped


def validate_endpoint_ownership(desired_node_slug: str, endpoint_node_slug: str) -> None:
    """Require an endpoint selected by a placement/config to belong to its node."""

    if desired_node_slug != endpoint_node_slug:
        raise ContractError(
            "endpoint_node_mismatch",
            f"endpoint belongs to {endpoint_node_slug!r}, not {desired_node_slug!r}",
        )


def actual_state_problem(
    collected_at: str | None,
    generated_at: str,
    *,
    max_age_hours: int = ACTUAL_MAX_AGE_HOURS,
) -> str | None:
    """Return a host-skip reason for missing, invalid, or stale actual data."""

    if not collected_at:
        return "missing_actual_data"
    try:
        collected = _parse_datetime(collected_at)
        generated = _parse_datetime(generated_at)
    except ValueError:
        return "invalid_actual_timestamp"
    if collected < generated - timedelta(hours=max_age_hours):
        return "stale_actual_data"
    return None


def resolve_connection_variables(
    *,
    inventory_hostname: str,
    actual_state_policy: str,
    connection_path: str,
    actual_local_ip: str | None = None,
    local_endpoint: Mapping[str, Any] | None = None,
    tailscale_endpoint: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve only the desired/actual connection variables allowed by schema 2.0."""

    variables: dict[str, Any] = {"connection_path": connection_path}
    local_endpoint = local_endpoint or {}
    tailscale_endpoint = tailscale_endpoint or {}
    if actual_local_ip:
        variables["local_ip"] = _normalize_ip(actual_local_ip, "actual_local_ip")
    elif actual_state_policy == "declared" and local_endpoint.get("ip_address"):
        variables["local_ip"] = _normalize_ip(local_endpoint["ip_address"], "local_endpoint.ip_address")
    if _nonempty(local_endpoint.get("dns_name")):
        variables["local_dns_hostname"] = local_endpoint["dns_name"].strip()
    if _nonempty(local_endpoint.get("mdns_name")):
        variables["mdns_hostname"] = local_endpoint["mdns_name"].strip()
    if tailscale_endpoint.get("ip_address"):
        variables["tailscale_ip"] = _normalize_ip(tailscale_endpoint["ip_address"], "tailscale_endpoint.ip_address")
    if connection_path == "local":
        candidates = (
            variables.get("local_ip"),
            variables.get("local_dns_hostname"),
            variables.get("mdns_hostname"),
            inventory_hostname,
        )
        variables["ansible_host"] = next(value for value in candidates if _nonempty(value))
    elif connection_path == "tailscale":
        if "tailscale_ip" not in variables:
            raise ContractError("unresolved_connection_path", "tailscale path requires a usable tailscale endpoint")
        variables["ansible_host"] = variables["tailscale_ip"]
    else:
        raise ContractError("unresolved_connection_path", f"unsupported connection path {connection_path!r}")
    return variables


def merge_host_variables(assignments: Iterable[tuple[str, Mapping[str, Any]]]) -> dict[str, Any]:
    """Merge mapped placement variables and fail on different values."""

    merged: dict[str, Any] = {}
    owners: dict[str, str] = {}
    for source, variables in assignments:
        for name in sorted(variables):
            if name in merged and merged[name] != variables[name]:
                raise ContractError(
                    "conflicting_host_variable",
                    f"{name!r} differs between {owners[name]!r} and {source!r}",
                )
            merged[name] = variables[name]
            owners.setdefault(name, source)
    return merged


def validate_production_inventory_document(
    value: Any,
    profiles: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the closed Ansible inventory envelope for production schema 2.0."""

    validated_profiles = validate_deployment_profiles(dict(profiles))
    if not isinstance(value, dict) or set(value) != {"all"} or not isinstance(value["all"], dict):
        raise ContractError("invalid_inventory_schema", "inventory root must contain only the all object")
    all_data = value["all"]
    _require_exact_keys(all_data, {"vars", "children"}, "all")
    metadata = all_data["vars"]
    if not isinstance(metadata, dict):
        raise ContractError("invalid_inventory_schema", "all.vars must be an object")
    _require_exact_keys(metadata, _INVENTORY_METADATA_KEYS, "all.vars")
    _validate_generation_metadata(
        schema_version=metadata["nintent_inventory_schema_version"],
        generation_id=metadata["nintent_generation_id"],
        generated_at=metadata["nintent_generated_at"],
        report_path=metadata["nintent_report_path"],
        digest=metadata["nintent_deployment_profile_digest"],
    )
    children = all_data["children"]
    if not isinstance(children, dict):
        raise ContractError("invalid_inventory_schema", "all.children must be an object")
    core_groups = {"ssh_hosts", "linux", "macos", "haos", "power_managed"}
    service_groups = {profile["group"] for profile in validated_profiles.values()}
    unknown_groups = set(children) - core_groups - service_groups
    missing_groups = core_groups - set(children)
    if unknown_groups or missing_groups:
        _raise_key_error(unknown_groups, missing_groups, "all.children")
    allowed_host_variables = set(_BASE_HOST_VARIABLES)
    for profile in validated_profiles.values():
        allowed_host_variables.update(
            definition["ansible_variable"] for definition in profile["variables"].values()
        )
    ssh_hosts: set[str] = set()
    for group_name in sorted(children):
        group = children[group_name]
        if not isinstance(group, dict):
            raise ContractError("invalid_inventory_schema", "group must be an object", path=f"all.children.{group_name}")
        _require_exact_keys(group, {"hosts"}, f"all.children.{group_name}")
        hosts = group["hosts"]
        if not isinstance(hosts, dict):
            raise ContractError("invalid_inventory_schema", "hosts must be an object", path=f"all.children.{group_name}.hosts")
        for hostname, host_vars in hosts.items():
            _require_slug(hostname, f"all.children.{group_name}.hosts")
            if not isinstance(host_vars, dict):
                raise ContractError("invalid_inventory_schema", "host value must be an object", path=f"all.children.{group_name}.hosts.{hostname}")
            if group_name == "ssh_hosts":
                unknown_variables = set(host_vars) - allowed_host_variables
                if unknown_variables:
                    raise ContractError(
                        "unknown_host_variable",
                        f"unknown variables: {', '.join(sorted(unknown_variables))}",
                        path=f"all.children.ssh_hosts.hosts.{hostname}",
                    )
                ssh_hosts.add(hostname)
            elif host_vars:
                raise ContractError(
                    "invalid_group_member",
                    "selector and service group members must use empty objects",
                    path=f"all.children.{group_name}.hosts.{hostname}",
                )
    dangling = sorted(
        hostname
        for group_name, group in children.items()
        if group_name != "ssh_hosts"
        for hostname in group["hosts"]
        if hostname not in ssh_hosts
    )
    if dangling:
        raise ContractError("dangling_group_member", f"hosts are not in ssh_hosts: {', '.join(dangling)}")
    return value


def validate_production_report(value: Any) -> dict[str, Any]:
    """Validate the closed companion-report envelope for schema 2.0."""

    if not isinstance(value, dict):
        raise ContractError("invalid_report_schema", "report must be an object")
    _require_exact_keys(value, _REPORT_KEYS, "report")
    _validate_generation_metadata(
        schema_version=value["schema_version"],
        generation_id=value["generation_id"],
        generated_at=value["generated_at"],
        report_path=value["report_path"],
        digest=value["deployment_profile_digest"],
    )
    summary = value["summary"]
    if not isinstance(summary, dict):
        raise ContractError("invalid_report_schema", "summary must be an object")
    _require_exact_keys(summary, _REPORT_SUMMARY_KEYS, "report.summary")
    if any(not isinstance(summary[key], int) or isinstance(summary[key], bool) or summary[key] < 0 for key in summary):
        raise ContractError("invalid_report_schema", "summary values must be non-negative integers")
    for key in ("hosts", "skipped", "drift", "errors"):
        if not isinstance(value[key], list):
            raise ContractError("invalid_report_schema", f"{key} must be an array")
    for index, host in enumerate(value["hosts"]):
        path = f"report.hosts[{index}]"
        if not isinstance(host, dict):
            raise ContractError("invalid_report_schema", "host must be an object", path=path)
        _require_exact_keys(host, _REPORT_HOST_KEYS, path)
        operational_values = host["operational_values"]
        if not isinstance(operational_values, dict):
            raise ContractError("invalid_report_schema", "operational_values must be an object", path=path)
        _require_exact_keys(operational_values, _OPERATIONAL_VALUE_KEYS, f"{path}.operational_values")
        for field_name, record in operational_values.items():
            _validate_value_record(record, f"{path}.operational_values.{field_name}")
    return value


def _validate_value_record(record: Any, path: str) -> None:
    if not isinstance(record, dict):
        raise ContractError("invalid_report_schema", "value record must be an object", path=path)
    _require_exact_keys(record, _VALUE_RECORD_KEYS, path)
    if record["source"] not in {"derived", "default", "override"}:
        raise ContractError("invalid_report_schema", "invalid operational source", path=f"{path}.source")
    if not isinstance(record["override_won"], bool):
        raise ContractError("invalid_report_schema", "override_won must be boolean", path=path)
    reference = record["source_reference"]
    if not isinstance(reference, dict) or not isinstance(reference.get("kind"), str):
        raise ContractError("invalid_report_schema", "source_reference must have a kind", path=path)
    allowed = {
        "override_presence": {"kind", "field"},
        "override_absence": {"kind", "field"},
        "nodeutils_observation": {"kind", "observed_system", "field", "collected_at"},
        "desired_endpoint": {
            "kind", "id", "name", "endpoint_type", "ip_address", "dns_name", "mdns_name", "address_field"
        },
        "operational_override": {"kind", "id", "field", "endpoint", "address_field"},
        "ansible_default": {"kind"},
        "safe_default": {"kind", "field"},
    }.get(reference["kind"])
    if allowed is None:
        raise ContractError("invalid_report_schema", "unknown source_reference kind", path=path)
    unknown = set(reference) - allowed
    if unknown:
        _raise_key_error(unknown, set(), f"{path}.source_reference")
    if reference["kind"] in {"override_presence", "override_absence", "safe_default"} and "field" not in reference:
        _raise_key_error(set(), {"field"}, f"{path}.source_reference")
    if reference["kind"] == "operational_override":
        _require_exact_subset(reference, {"kind", "id", "field"}, f"{path}.source_reference")
        endpoint = reference.get("endpoint")
        if endpoint is not None:
            _validate_endpoint_reference(endpoint, f"{path}.source_reference.endpoint")
    if reference["kind"] == "desired_endpoint":
        _validate_endpoint_reference(reference, f"{path}.source_reference", extra={"kind", "address_field"})


def _require_exact_subset(value: Mapping[str, Any], required: set[str], path: str) -> None:
    missing = required - set(value)
    if missing:
        _raise_key_error(set(), missing, path)


def _validate_endpoint_reference(value: Mapping[str, Any], path: str, *, extra: set[str] = frozenset()) -> None:
    required = {"id", "name", "endpoint_type", "ip_address", "dns_name", "mdns_name"}
    allowed = required | set(extra)
    unknown = set(value) - allowed
    missing = required - set(value)
    if unknown or missing:
        _raise_key_error(unknown, missing, path)


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], path: str) -> None:
    unknown = set(value) - expected
    missing = expected - set(value)
    if unknown or missing:
        _raise_key_error(unknown, missing, path)


def _raise_key_error(unknown: set[str], missing: set[str], path: str) -> None:
    details = []
    if missing:
        details.append(f"missing keys: {', '.join(sorted(missing))}")
    if unknown:
        details.append(f"unknown keys: {', '.join(sorted(unknown))}")
    raise ContractError("invalid_contract_keys", "; ".join(details), path=path)


def _require_slug(value: Any, path: str) -> None:
    if not isinstance(value, str) or not _SLUG_RE.fullmatch(value):
        raise ContractError("invalid_slug", "must be a lowercase slug", path=path)


def _matches_json_type(value: Any, value_type: str, item_type: str | None) -> bool:
    if value_type == "string":
        return isinstance(value, str)
    if value_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if value_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if value_type == "boolean":
        return isinstance(value, bool)
    if value_type == "list":
        return isinstance(value, list) and all(_matches_json_type(item, item_type or "", None) for item in value)
    return False


def _require_string_mapping_keys(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ContractError("invalid_profile_json", "all mapping keys must be strings", path=path)
            _require_string_mapping_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _require_string_mapping_keys(item, f"{path}[{index}]")


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def _validate_generation_metadata(
    *,
    schema_version: Any,
    generation_id: Any,
    generated_at: Any,
    report_path: Any,
    digest: Any,
) -> None:
    if schema_version != PRODUCTION_INVENTORY_SCHEMA_VERSION:
        raise ContractError("unsupported_inventory_schema", f"expected schema {PRODUCTION_INVENTORY_SCHEMA_VERSION}")
    try:
        parsed_uuid = uuid.UUID(str(generation_id))
    except (ValueError, AttributeError) as exc:
        raise ContractError("invalid_generation_id", "generation_id must be a UUID") from exc
    if str(parsed_uuid) != generation_id:
        raise ContractError("invalid_generation_id", "generation_id must be a canonical lowercase UUID")
    try:
        _parse_datetime(generated_at)
    except (TypeError, ValueError) as exc:
        raise ContractError("invalid_generated_at", "generated_at must be timezone-aware RFC3339") from exc
    expected_path = f"production.reports/{generation_id}.json"
    if report_path != expected_path:
        raise ContractError("invalid_report_path", f"report_path must be {expected_path!r}")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ContractError("invalid_profile_digest", "digest must be 64 lowercase hexadecimal characters")


def _normalize_ip(value: Any, path: str) -> str:
    try:
        return str(ipaddress.ip_interface(str(value)).ip)
    except ValueError as exc:
        raise ContractError("invalid_connection_address", "must be an IP address", path=path) from exc


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())
