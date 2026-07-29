"""Braindump's code-carrying domain errors."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nctl_core.nautobot import NautobotError


class BraindumpError(NautobotError):
    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None) -> None:
        self.code = code
        self.detail = detail or {}
        super().__init__(message)


def error(code: str, message: str, detail: dict[str, Any] | None = None) -> BraindumpError:
    """Build the sole Braindump error type while retaining its public code payload."""
    return BraindumpError(code, message, detail)


def invalid_braindump_id_error(value: str) -> BraindumpError:
    return error("invalid_braindump_id", f"not a valid Braindump UUID: {value!r}", {"value": value})


def invalid_supersede_old_ids_error(reason: str) -> BraindumpError:
    return error("invalid_supersede_old_ids", reason)


def invalid_authorship_error(value: str, allowed: tuple[str, ...]) -> BraindumpError:
    return error("invalid_authorship", f"invalid authorship {value!r}; must be one of {', '.join(allowed)}", {"value": value, "allowed": list(allowed)})


def invalid_text_error(field_name: str) -> BraindumpError:
    return error("invalid_text", f"{field_name} must not be empty or whitespace-only", {"field": field_name})


def input_conflict_error(field_name: str, *, both: bool) -> BraindumpError:
    reason = "both provided" if both else "neither provided"
    return error("input_conflict", f"exactly one of literal {field_name} or --file is required ({reason})", {"field": field_name})


def input_file_error(path: Path, reason: str) -> BraindumpError:
    return error("input_file_error", f"cannot read {path}: {reason}", {"path": str(path)})


def input_file_invalid_utf8_error(path: Path) -> BraindumpError:
    return error("input_file_invalid_utf8", f"{path} is not valid UTF-8", {"path": str(path)})


def braindump_not_found_error(braindump_id: str) -> BraindumpError:
    return error("braindump_not_found", f"no Braindump with id {braindump_id!r}", {"braindump_id": braindump_id})


def braindump_validation_failed_error(status_code: int, detail_text: str) -> BraindumpError:
    return error("braindump_validation_failed", f"Braindump write rejected as invalid: HTTP {status_code}", {"status_code": status_code, "detail": detail_text[:200]})


def braindump_write_rejected_error(status_code: int, detail_text: str) -> BraindumpError:
    return error("braindump_write_rejected", f"Braindump write rejected: HTTP {status_code}", {"status_code": status_code, "detail": detail_text[:200]})


def braindump_confirmation_mismatch_error(braindump_id: str) -> BraindumpError:
    return error("braindump_confirmation_mismatch", f"GraphQL refetch of Braindump {braindump_id!r} did not match the requested write", {"braindump_id": braindump_id})


def supersede_validation_failed_error(status_code: int, detail_text: str) -> BraindumpError:
    return error("braindump_supersede_invalid", f"Braindump supersession rejected as invalid: HTTP {status_code}", {"status_code": status_code, "detail": detail_text[:200]})


def supersede_write_rejected_error(status_code: int, detail_text: str) -> BraindumpError:
    return error("braindump_supersede_rejected", f"Braindump supersession rejected: HTTP {status_code}", {"status_code": status_code, "detail": detail_text[:200]})


def supersede_confirmation_mismatch_error(braindump_id: str) -> BraindumpError:
    return error("braindump_supersede_confirmation_mismatch", f"GraphQL refetch of replacement Braindump {braindump_id!r} did not confirm supersession", {"braindump_id": braindump_id})


def review_validation_failed_error(status_code: int, detail_text: str) -> BraindumpError:
    return error("review_validation_failed", f"Alignment review write rejected as invalid: HTTP {status_code}", {"status_code": status_code, "detail": detail_text[:200]})


def review_write_rejected_error(status_code: int, detail_text: str) -> BraindumpError:
    return error("review_write_rejected", f"Alignment review write rejected: HTTP {status_code}", {"status_code": status_code, "detail": detail_text[:200]})


def review_confirmation_mismatch_error(braindump_id: str) -> BraindumpError:
    return error("review_confirmation_mismatch", f"GraphQL refetch of Braindump {braindump_id!r} did not show the requested review", {"braindump_id": braindump_id})


def review_delete_rejected_error(status_code: int, detail_text: str) -> BraindumpError:
    return error("review_delete_rejected", f"Alignment review delete rejected: HTTP {status_code}", {"status_code": status_code, "detail": detail_text[:200]})


def delete_confirmation_mismatch_error(target: str, target_id: str) -> BraindumpError:
    return error("delete_confirmation_mismatch", f"GraphQL refetch still shows {target} {target_id!r} after delete", {"target": target, "target_id": target_id})


def write_error(status_code: int, detail_text: str) -> BraindumpError:
    if status_code == 400:
        return braindump_validation_failed_error(status_code, detail_text)
    return braindump_write_rejected_error(status_code, detail_text)


def review_write_error(status_code: int, detail_text: str) -> BraindumpError:
    if status_code == 400:
        return review_validation_failed_error(status_code, detail_text)
    return review_write_rejected_error(status_code, detail_text)
