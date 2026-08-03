"""WorkflowEpisode's code-carrying domain errors."""

from __future__ import annotations

from typing import Any

from nctl_core.nautobot import NautobotError


class WorkflowEpisodeError(NautobotError):
    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None) -> None:
        self.code = code
        self.detail = detail or {}
        super().__init__(message)


def error(code: str, message: str, detail: dict[str, Any] | None = None) -> WorkflowEpisodeError:
    """Build the sole WorkflowEpisode error type while retaining its public code payload."""
    return WorkflowEpisodeError(code, message, detail)


def invalid_workflow_episode_id_error(value: str) -> WorkflowEpisodeError:
    return error("invalid_workflow_episode_id", f"not a valid WorkflowEpisode UUID: {value!r}", {"value": value})


def workflow_episode_not_found_error(episode_id: str) -> WorkflowEpisodeError:
    return error("workflow_episode_not_found", f"no WorkflowEpisode with id {episode_id!r}", {"episode_id": episode_id})


def invalid_text_error(field_name: str) -> WorkflowEpisodeError:
    return error("invalid_text", f"{field_name} must not be empty or whitespace-only", {"field": field_name})


def input_conflict_error(field_name: str, *, both: bool) -> WorkflowEpisodeError:
    reason = "both provided" if both else "neither provided"
    return error("input_conflict", f"exactly one of literal {field_name} or --file is required ({reason})", {"field": field_name})


def input_file_error(path: str, reason: str) -> WorkflowEpisodeError:
    return error("input_file_error", f"cannot read {path}: {reason}", {"path": path})


def input_file_invalid_utf8_error(path: str) -> WorkflowEpisodeError:
    return error("input_file_invalid_utf8", f"{path} is not valid UTF-8", {"path": path})


def invalid_json_error(field_name: str, reason: str) -> WorkflowEpisodeError:
    return error("invalid_json", f"{field_name} is not valid JSON: {reason}", {"field": field_name})


def invalid_namespace_payload_error(field_name: str) -> WorkflowEpisodeError:
    return error("invalid_namespace_payload", f"{field_name} must be a JSON object", {"field": field_name})


def invalid_status_filter_error(value: str, allowed: tuple[str, ...]) -> WorkflowEpisodeError:
    return error(
        "invalid_status_filter",
        f"invalid status {value!r}; must be one of {', '.join(allowed)}",
        {"value": value, "allowed": list(allowed)},
    )


def workflow_episode_create_rejected_error(status_code: int, detail_text: str) -> WorkflowEpisodeError:
    if status_code == 400:
        return error("workflow_episode_validation_failed", f"WorkflowEpisode create rejected as invalid: HTTP {status_code}", {"status_code": status_code, "detail": detail_text[:200]})
    return error("workflow_episode_create_rejected", f"WorkflowEpisode create rejected: HTTP {status_code}", {"status_code": status_code, "detail": detail_text[:200]})


def workflow_episode_write_rejected_error(status_code: int, detail_text: str) -> WorkflowEpisodeError:
    if status_code == 400:
        return error("workflow_episode_validation_failed", f"WorkflowEpisode write rejected as invalid: HTTP {status_code}", {"status_code": status_code, "detail": detail_text[:200]})
    return error("workflow_episode_write_rejected", f"WorkflowEpisode write rejected: HTTP {status_code}", {"status_code": status_code, "detail": detail_text[:200]})


def workflow_episode_transition_ineligible_error(episode_id: str, new_status: str, detail_text: str) -> WorkflowEpisodeError:
    return error(
        "workflow_episode_transition_ineligible",
        f"WorkflowEpisode {episode_id!r} cannot transition to {new_status!r} from its current status",
        {"episode_id": episode_id, "new_status": new_status, "detail": detail_text[:200]},
    )


def workflow_episode_transition_rejected_error(status_code: int, detail_text: str) -> WorkflowEpisodeError:
    return error("workflow_episode_transition_rejected", f"WorkflowEpisode transition rejected: HTTP {status_code}", {"status_code": status_code, "detail": detail_text[:200]})
