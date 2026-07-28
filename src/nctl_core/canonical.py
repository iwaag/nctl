"""Deterministic JSON serialization shared across domain packages."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from nctl_core.production.contract import ContractError


def canonical_json(value: Any) -> str:
    """Serialize a JSON value with stable UTF-8 bytes and strict mapping keys."""

    _require_string_mapping_keys(value)
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ContractError("invalid_profile_json", str(exc)) from exc


def canonical_json_digest(value: Any) -> str:
    """Return the SHA-256 digest of canonical UTF-8 JSON bytes."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _require_string_mapping_keys(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ContractError("invalid_profile_json", "all mapping keys must be strings", path=path)
            _require_string_mapping_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _require_string_mapping_keys(item, f"{path}[{index}]")
