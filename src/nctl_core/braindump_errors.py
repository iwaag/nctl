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


class InvalidBraindumpIdError(BraindumpError):
    def __init__(self, value: str) -> None:
        super().__init__("invalid_braindump_id", f"not a valid Braindump UUID: {value!r}", {"value": value})


class InvalidAuthorshipError(BraindumpError):
    def __init__(self, value: str, allowed: tuple[str, ...]) -> None:
        super().__init__("invalid_authorship", f"invalid authorship {value!r}; must be one of {', '.join(allowed)}", {"value": value, "allowed": list(allowed)})


class InvalidTextError(BraindumpError):
    def __init__(self, field_name: str) -> None:
        super().__init__("invalid_text", f"{field_name} must not be empty or whitespace-only", {"field": field_name})


class InputConflictError(BraindumpError):
    def __init__(self, field_name: str, *, both: bool) -> None:
        reason = "both provided" if both else "neither provided"
        super().__init__("input_conflict", f"exactly one of literal {field_name} or --file is required ({reason})", {"field": field_name})


class NoUpdateFieldsError(BraindumpError):
    def __init__(self, braindump_id: str) -> None:
        super().__init__("no_update_fields", "update requires at least one changed field (title, authorship, or body)", {"braindump_id": braindump_id})


class InputFileError(BraindumpError):
    def __init__(self, path: Path, reason: str) -> None:
        super().__init__("input_file_error", f"cannot read {path}: {reason}", {"path": str(path)})


class InputFileInvalidUtf8Error(BraindumpError):
    def __init__(self, path: Path) -> None:
        super().__init__("input_file_invalid_utf8", f"{path} is not valid UTF-8", {"path": str(path)})


class BraindumpNotFoundError(BraindumpError):
    def __init__(self, braindump_id: str) -> None:
        super().__init__("braindump_not_found", f"no Braindump with id {braindump_id!r}", {"braindump_id": braindump_id})


class BraindumpValidationFailedError(BraindumpError):
    def __init__(self, status_code: int, detail_text: str) -> None:
        super().__init__("braindump_validation_failed", f"Braindump write rejected as invalid: HTTP {status_code}", {"status_code": status_code, "detail": detail_text[:200]})


class BraindumpWriteRejectedError(BraindumpError):
    def __init__(self, status_code: int, detail_text: str) -> None:
        super().__init__("braindump_write_rejected", f"Braindump write rejected: HTTP {status_code}", {"status_code": status_code, "detail": detail_text[:200]})


class BraindumpConfirmationMismatchError(BraindumpError):
    def __init__(self, braindump_id: str) -> None:
        super().__init__("braindump_confirmation_mismatch", f"GraphQL refetch of Braindump {braindump_id!r} did not match the requested write", {"braindump_id": braindump_id})


class ReviewValidationFailedError(BraindumpError):
    def __init__(self, status_code: int, detail_text: str) -> None:
        super().__init__("review_validation_failed", f"Alignment review write rejected as invalid: HTTP {status_code}", {"status_code": status_code, "detail": detail_text[:200]})


class ReviewWriteRejectedError(BraindumpError):
    def __init__(self, status_code: int, detail_text: str) -> None:
        super().__init__("review_write_rejected", f"Alignment review write rejected: HTTP {status_code}", {"status_code": status_code, "detail": detail_text[:200]})


class ReviewConfirmationMismatchError(BraindumpError):
    def __init__(self, braindump_id: str) -> None:
        super().__init__("review_confirmation_mismatch", f"GraphQL refetch of Braindump {braindump_id!r} did not show the requested review", {"braindump_id": braindump_id})


class BraindumpDeleteRejectedError(BraindumpError):
    def __init__(self, status_code: int, detail_text: str) -> None:
        super().__init__("braindump_delete_rejected", f"Braindump delete rejected: HTTP {status_code}", {"status_code": status_code, "detail": detail_text[:200]})


class ReviewDeleteRejectedError(BraindumpError):
    def __init__(self, status_code: int, detail_text: str) -> None:
        super().__init__("review_delete_rejected", f"Alignment review delete rejected: HTTP {status_code}", {"status_code": status_code, "detail": detail_text[:200]})


class DeleteConfirmationMismatchError(BraindumpError):
    def __init__(self, target: str, target_id: str) -> None:
        super().__init__("delete_confirmation_mismatch", f"GraphQL refetch still shows {target} {target_id!r} after delete", {"target": target, "target_id": target_id})


def write_error(status_code: int, detail_text: str) -> BraindumpError:
    if status_code == 400:
        return BraindumpValidationFailedError(status_code, detail_text)
    return BraindumpWriteRejectedError(status_code, detail_text)


def review_write_error(status_code: int, detail_text: str) -> BraindumpError:
    if status_code == 400:
        return ReviewValidationFailedError(status_code, detail_text)
    return ReviewWriteRejectedError(status_code, detail_text)
