"""Common JSON output envelope (`nctl.<command>.v1`). See docs/output-format.md."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T", bound=BaseModel)


class EnvelopeError(BaseModel):
    code: str
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)


class Envelope(BaseModel, Generic[T]):
    model_config = ConfigDict(populate_by_name=True)

    schema_name: str = Field(alias="schema")
    generated_at: datetime
    ok: bool
    data: T
    errors: list[EnvelopeError] = Field(default_factory=list)

    @classmethod
    def build(cls, schema: str, data: T, errors: list[EnvelopeError] | None = None) -> "Envelope[T]":
        errors = errors or []
        return cls(schema=schema, generated_at=datetime.now(timezone.utc), ok=not errors, data=data, errors=errors)

    def to_json(self) -> str:
        return self.model_dump_json(by_alias=True, indent=2)


def emit(envelope: Envelope, json_mode: bool, render_text: Callable[[Envelope], str]) -> None:
    """Print an envelope: the raw JSON document in --json mode, else its text rendering.

    Diagnostics never belong here — callers write those to stderr separately.
    """
    print(envelope.to_json() if json_mode else render_text(envelope))
