"""REST transport for Braindump and Alignment Review writes."""

from __future__ import annotations

from typing import Any

from nctl_core.braindump_errors import (
    BraindumpDeleteRejectedError,
    ReviewDeleteRejectedError,
    review_write_error,
    write_error,
)
from nctl_core.nautobot import NautobotClient

BRAINDUMP_API_BASE = "/api/plugins/intent-catalog/braindumps"
ALIGNMENT_REVIEW_API_BASE = "/api/plugins/intent-catalog/alignment-reviews"


def create_braindump(client: NautobotClient, payload: dict[str, Any]) -> str:
    response = client.rest_post(f"{BRAINDUMP_API_BASE}/", payload)
    if not response.is_success:
        raise write_error(response.status_code, response.text)
    return response.json()["id"]


def update_braindump(client: NautobotClient, braindump_id: str, payload: dict[str, Any]) -> None:
    response = client.rest_patch(f"{BRAINDUMP_API_BASE}/{braindump_id}/", payload)
    if not response.is_success:
        raise write_error(response.status_code, response.text)


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


def delete_braindump(client: NautobotClient, braindump_id: str) -> None:
    response = client.rest_delete(f"{BRAINDUMP_API_BASE}/{braindump_id}/")
    if not response.is_success:
        raise BraindumpDeleteRejectedError(response.status_code, response.text)


def delete_review(client: NautobotClient, review_id: str) -> None:
    response = client.rest_delete(f"{ALIGNMENT_REVIEW_API_BASE}/{review_id}/")
    if not response.is_success:
        raise ReviewDeleteRejectedError(response.status_code, response.text)
