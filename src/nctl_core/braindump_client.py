"""REST transport for Braindump and Alignment Review writes."""

from __future__ import annotations

from typing import Any

from nctl_core.braindump_errors import (
    braindump_purge_ineligible_error,
    braindump_purge_rejected_error,
    review_delete_rejected_error,
    review_write_error,
    write_error,
    supersede_validation_failed_error,
    supersede_write_rejected_error,
)
from nctl_core.nautobot import NautobotClient

BRAINDUMP_API_BASE = "/api/plugins/intent-catalog/braindumps"
ALIGNMENT_REVIEW_API_BASE = "/api/plugins/intent-catalog/alignment-reviews"


def create_braindump(client: NautobotClient, payload: dict[str, Any]) -> str:
    response = client.rest_post(f"{BRAINDUMP_API_BASE}/", payload)
    if not response.is_success:
        raise write_error(response.status_code, response.text)
    return response.json()["id"]


def supersede_braindumps(client: NautobotClient, payload: dict[str, Any]) -> tuple[str, list[str]]:
    response = client.rest_post(f"{BRAINDUMP_API_BASE}/supersede/", payload)
    if response.status_code == 400:
        raise supersede_validation_failed_error(response.status_code, response.text)
    if not response.is_success:
        raise supersede_write_rejected_error(response.status_code, response.text)
    data = response.json()
    return data["braindump"]["id"], data["superseded_ids"]


def create_review(client: NautobotClient, braindump_id: str, summary: str):
    response = client.rest_post(f"{ALIGNMENT_REVIEW_API_BASE}/", {"braindump": braindump_id, "summary": summary})
    if response.status_code == 400:
        return response
    if not response.is_success:
        raise review_write_error(response.status_code, response.text)
    return response


def update_review(client: NautobotClient, review_id: str, summary: str) -> None:
    response = client.rest_patch(f"{ALIGNMENT_REVIEW_API_BASE}/{review_id}/", {"summary": summary})
    if not response.is_success:
        raise review_write_error(response.status_code, response.text)


def delete_review(client: NautobotClient, review_id: str) -> None:
    response = client.rest_delete(f"{ALIGNMENT_REVIEW_API_BASE}/{review_id}/")
    if not response.is_success:
        raise review_delete_rejected_error(response.status_code, response.text)


def purge_braindump(client: NautobotClient, braindump_id: str, *, apply: bool) -> dict[str, Any]:
    """Ask the dedicated endpoint to plan or execute one exact purge."""
    url = f"{BRAINDUMP_API_BASE}/{braindump_id}/purge/"
    response = client.rest_delete(url) if apply else client.rest_post(url, {})
    if response.status_code == 409:
        raise braindump_purge_ineligible_error(braindump_id, response.text)
    if not response.is_success:
        raise braindump_purge_rejected_error(response.status_code, response.text)
    return response.json()
