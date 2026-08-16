"""Shared types for the LLM gateway.

Routing is by **task class**, never a hardcoded model id at the call site.
That is what makes BYO-key real: switching provider or model is a config
change, and the code that mines a hook or writes a subject line never
names a model at all.

The task taxonomy also encodes the product's economics. The prior work
measured $0.03/lead for research on a cheap model and a 51-lead copy run
under a dollar; that only holds if volume work (EXTRACT, CLASSIFY) routes
to cheap models and only the low-volume, quality-visible work (STRATEGY,
COPY, CRITIQUE) reaches an expensive one.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


class Task(enum.StrEnum):
    EXTRACT = "extract"  # high volume, cheap model
    CLASSIFY = "classify"  # high volume, cheap model
    RESEARCH = "research"  # tool-using loop, mid model
    STRATEGY = "strategy"  # low volume, strong model
    COPY = "copy"  # low volume, strong model
    CRITIQUE = "critique"  # low volume, strong model


class Role(enum.StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class FinishReason(enum.StrEnum):
    STOP = "stop"
    LENGTH = "length"
    TOOL_CALLS = "tool_calls"
    CONTENT_FILTER = "content_filter"


@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    content: str
    tool_call_id: str | None = None
    name: str | None = None


@dataclass(frozen=True, slots=True)
class Evidence:
    """Untrusted fetched content.

    A separate type from Message on purpose. Evidence can only enter a
    call through `complete(evidence=...)`, which wraps it — there is no
    code path that lets a caller drop scraped text straight into the
    instruction section. See app/llm/safety.py.
    """

    content: str
    source_url: str


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    parameters: Mapping[str, Any]  # JSON Schema


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Usage:
    """Normalised token counts.

    Providers disagree on the field names (prompt_tokens vs input_tokens
    vs promptTokenCount); every adapter converts to this before the
    gateway sees it, so cost accounting has exactly one shape to reason
    about.
    """

    prompt_tokens: int
    completion_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    """What every adapter returns, regardless of provider."""

    text: str
    model: str
    usage: Usage
    finish_reason: FinishReason
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass(frozen=True, slots=True)
class Result[T]:
    """One completed gateway call.

    `value` is the validated schema instance when a schema was requested,
    and None otherwise — it is never a partially-parsed object. If the
    schema could not be satisfied, this object does not exist; a
    SchemaValidationError was raised instead.
    """

    text: str
    model: str
    provider: str
    task: Task
    usage: Usage
    cost_usd: Decimal
    latency_ms: int
    value: T | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_rounds: int = 0
    # Non-fatal problems the caller should see rather than have buried in
    # a log line — e.g. stale price data, or a model with no known price.
    warnings: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class Estimate:
    task: Task
    n_leads: int
    avg_cost_usd: Decimal
    total_usd: Decimal
    based_on_calls: int

    @property
    def is_measured(self) -> bool:
        """False when this is a prior rather than measured history — the
        UI must not present a guess as if it were data.
        """
        return self.based_on_calls > 0


def assistant(content: str) -> Message:
    return Message(role=Role.ASSISTANT, content=content)


def user(content: str) -> Message:
    return Message(role=Role.USER, content=content)


def system(content: str) -> Message:
    return Message(role=Role.SYSTEM, content=content)


def tool_result(content: str, *, tool_call_id: str, name: str) -> Message:
    return Message(role=Role.TOOL, content=content, tool_call_id=tool_call_id, name=name)


__all__ = [
    "Estimate",
    "Evidence",
    "FinishReason",
    "Message",
    "ProviderResponse",
    "Result",
    "Role",
    "Sequence",
    "Task",
    "Tool",
    "ToolCall",
    "Usage",
    "assistant",
    "system",
    "tool_result",
    "user",
]
