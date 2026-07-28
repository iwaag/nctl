"""Contracts and shared evidence construction at the action boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field

from nctl_core.ansible import CommandRunner
from nctl_core.artifacts import OperationArtifacts
from nctl_core.config import Config
from nctl_core.events import OperationLog
from nctl_core.nautobot import NautobotClient
from nctl_core.output import EnvelopeError
from nctl_core.sources.snapshot import SourceSnapshot
from nctl_core.ssh_enroll import SshProbeRunner

from ..model import ReconcileAction
from ..results import ActionResult


@dataclass(frozen=True)
class ActionContext:
    cfg: Config
    operation_log: OperationLog
    artifacts: OperationArtifacts
    round_index: int
    snapshot: SourceSnapshot
    client: NautobotClient | None
    now: Callable[[], datetime]
    command_runner: CommandRunner | None
    ssh_probe: SshProbeRunner | None
    generated_at: str


class ExecutedAction(BaseModel):
    result: ActionResult
    terminal_errors: list[EnvelopeError] = Field(default_factory=list)


@dataclass(frozen=True)
class ActionHandler:
    reconciler_id: str
    execute: Callable[[ActionContext, ReconcileAction], ExecutedAction]
    phase: Literal["bootstrap", "service"]
    needs_client: bool


def actuation_result(
    context: ActionContext,
    action: ReconcileAction,
    target_slugs: list[str],
    success: bool,
    detail: dict[str, Any],
    *,
    requires_observation: bool,
    mutated: bool | None = None,
) -> ActionResult:
    context.operation_log.emit(
        "action_completed",
        f"action {action.id} completed",
        action_id=action.id,
        reconciler_id=action.reconciler_id,
        success=success,
    )
    context.operation_log.emit(
        "actuation_completed",
        f"actuation {action.id} completed",
        target_slugs=target_slugs,
        claimed_diff_codes=action.claimed_diff_codes,
        requires_observation=requires_observation,
        success=success,
    )
    return ActionResult(
        action_id=action.id,
        reconciler_id=action.reconciler_id,
        action_kind=action.action_kind,
        target_slugs=target_slugs,
        success=success,
        detail=detail,
        mutated=success if mutated is None else mutated,
    )


def failed_action_result(
    context: ActionContext, action: ReconcileAction, target_slugs: list[str], detail: dict[str, Any], message: str
) -> ActionResult:
    context.operation_log.emit(
        "action_completed",
        f"action {action.id} failed",
        level="error",
        action_id=action.id,
        reconciler_id=action.reconciler_id,
        success=False,
    )
    return ActionResult(
        action_id=action.id,
        reconciler_id=action.reconciler_id,
        action_kind=action.action_kind,
        target_slugs=target_slugs,
        success=False,
        detail=detail,
        error=message,
    )
