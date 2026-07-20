"""`nctl lifecycle NODE STATE` (Phase 3 Step 3.2, plan.md Decision 2).

A direct, idempotent setter for `DesiredNode.lifecycle` -- not an approval engine and not part of
`reconcile --yes`. Reads follow the project-wide GraphQL convention
(`nctl_core.sources.desired.fetch_desired_snapshot`); the single write is a partial REST PATCH
through the existing intent-catalog ViewSet so unrelated node fields are never touched. Every write
is confirmed by a GraphQL refetch before `changed=True` is reported; a mismatch fails closed rather
than claiming success (plan.md Decision 2, steps 4-5).

No entry is added to `drift.registry` or `reconcile.classify.CODE_CLASSIFICATION`: these are
command-scoped errors, not desired-vs-actual facts (plan.md Decision 3).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from nctl_core.config import Config, ConfigError
from nctl_core.nautobot import NautobotClient, NautobotError
from nctl_core.output import Envelope, EnvelopeError
from nctl_core.sources.desired import fetch_desired_snapshot

LIFECYCLE_SCHEMA = "nctl.lifecycle.v1"
INTENT_API_BASE = "/api/plugins/intent-catalog"

LIFECYCLE_STATES: tuple[str, ...] = ("planned", "approved", "active", "deprecated", "retired")


class LifecycleError(NautobotError):
    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None) -> None:
        self.code = code
        self.detail = detail or {}
        super().__init__(message)


class InvalidLifecycleError(LifecycleError):
    def __init__(self, requested_state: str) -> None:
        super().__init__(
            "invalid_lifecycle",
            f"invalid lifecycle {requested_state!r}; must be one of {', '.join(LIFECYCLE_STATES)}",
            {"requested_state": requested_state, "allowed": list(LIFECYCLE_STATES)},
        )


class UnknownNodeError(LifecycleError):
    def __init__(self, node_slug: str) -> None:
        super().__init__("unknown_node", f"no desired node with slug {node_slug!r}", {"node_slug": node_slug})


class LifecycleUpdateRejectedError(LifecycleError):
    def __init__(self, node_slug: str, requested_state: str, status_code: int, detail_text: str) -> None:
        super().__init__(
            "lifecycle_update_rejected",
            f"PATCH lifecycle={requested_state!r} on DesiredNode {node_slug!r} failed: HTTP {status_code}",
            {"node_slug": node_slug, "requested_state": requested_state, "status_code": status_code, "detail": detail_text[:200]},
        )


class LifecycleConfirmationMismatchError(LifecycleError):
    def __init__(self, node_slug: str, requested_state: str, confirmed_state: str | None) -> None:
        super().__init__(
            "lifecycle_confirmation_mismatch",
            f"expected DesiredNode {node_slug!r}.lifecycle={requested_state!r}, refetch shows {confirmed_state!r}",
            {"node_slug": node_slug, "requested_state": requested_state, "confirmed_state": confirmed_state},
        )


class LifecycleData(BaseModel):
    node_id: str
    node_slug: str
    previous_state: str
    requested_state: str
    current_state: str
    changed: bool


def _resolve_node(client: NautobotClient, node_slug: str):
    snapshot = fetch_desired_snapshot(client)
    matches = [node for node in snapshot.nodes if node.slug == node_slug]
    if not matches:
        raise UnknownNodeError(node_slug)
    return matches[0]


def set_node_lifecycle(client: NautobotClient, node_slug: str, requested_state: str) -> LifecycleData:
    """Pure operation: resolve, no-op if already matching, else PATCH and confirm.

    Raises a `LifecycleError` subclass on any failure; never returns a partial/unconfirmed result.
    """

    if requested_state not in LIFECYCLE_STATES:
        raise InvalidLifecycleError(requested_state)

    node = _resolve_node(client, node_slug)
    previous_state = node.lifecycle

    if previous_state == requested_state:
        return LifecycleData(
            node_id=node.id,
            node_slug=node.slug,
            previous_state=previous_state,
            requested_state=requested_state,
            current_state=previous_state,
            changed=False,
        )

    response = client.rest_patch(f"{INTENT_API_BASE}/nodes/{node.id}/", {"lifecycle": requested_state})
    if not response.is_success:
        raise LifecycleUpdateRejectedError(node_slug, requested_state, response.status_code, response.text)

    confirmed = _resolve_node(client, node_slug)
    if confirmed.lifecycle != requested_state:
        raise LifecycleConfirmationMismatchError(node_slug, requested_state, confirmed.lifecycle)

    return LifecycleData(
        node_id=node.id,
        node_slug=node.slug,
        previous_state=previous_state,
        requested_state=requested_state,
        current_state=confirmed.lifecycle,
        changed=True,
    )


def build_lifecycle(cfg: Config, node_slug: str, requested_state: str) -> Envelope[LifecycleData]:
    """CLI-facing entry point: resolves config/token, runs the operation, and always returns an
    envelope (never raises) so the thin Typer command only has to render and pick an exit code.
    """

    try:
        token = cfg.nautobot.resolve_token()
    except ConfigError as exc:
        return Envelope.build(
            LIFECYCLE_SCHEMA,
            _empty_data(node_slug, requested_state),
            [EnvelopeError(code="nautobot_token_error", message=str(exc))],
        )

    client = NautobotClient(cfg.nautobot.url, token)
    try:
        data = set_node_lifecycle(client, node_slug, requested_state)
    except LifecycleError as exc:
        return Envelope.build(
            LIFECYCLE_SCHEMA,
            _empty_data(node_slug, requested_state),
            [EnvelopeError(code=exc.code, message=str(exc), detail=exc.detail)],
        )
    except NautobotError as exc:
        return Envelope.build(
            LIFECYCLE_SCHEMA,
            _empty_data(node_slug, requested_state),
            [EnvelopeError(code="nautobot_connection_error", message=str(exc))],
        )
    finally:
        client.close()

    return Envelope.build(LIFECYCLE_SCHEMA, data)


def _empty_data(node_slug: str, requested_state: str) -> LifecycleData:
    return LifecycleData(
        node_id="",
        node_slug=node_slug,
        previous_state="",
        requested_state=requested_state,
        current_state="",
        changed=False,
    )


def render_lifecycle_text(envelope: Envelope[LifecycleData]) -> str:
    if not envelope.ok:
        lines = [f"error: {error.message}" for error in envelope.errors]
        return "\n".join(lines)
    data = envelope.data
    if not data.changed:
        return f"{data.node_slug}: already {data.current_state} (no change)"
    return f"{data.node_slug}: {data.previous_state} -> {data.current_state}"
