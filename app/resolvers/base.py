"""The core types every resolver and the executor share.

This is the whole abstraction the waterfall is built on: a Resolver takes a
LeadContext and returns Candidates, tagged with a Tier that determines when
it gets to run. See doc/plans/session-03-waterfall.md for the rationale —
this is Clay's business model as ~80 lines of code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import IntEnum
from typing import Any, Protocol, runtime_checkable
from uuid import UUID


def utcnow() -> datetime:
    return datetime.now(UTC)


class Tier(IntEnum):
    """Waterfall order. The executor always tries these in ascending order
    and only escalates when the lower tier left confidence below threshold.
    """

    FREE = 0
    OWNED = 1
    METERED = 2


@dataclass(frozen=True, slots=True)
class Candidate:
    """One resolver's answer for one field. Immutable — mutating a candidate
    during merge causes bugs that only show up under concurrency (see
    session gotchas).
    """

    value: str
    confidence: float
    source: str
    source_url: str | None = None
    retrieved_at: datetime = field(default_factory=utcnow)
    # Candidates sharing an independence_key are correlated (e.g. two SERP
    # queries against the same search index) and must be collapsed to their
    # best member before confidence merging — otherwise stacking them
    # manufactures false confidence. See app/resolvers/merge.py.
    independence_key: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence {self.confidence} outside [0,1]")


@dataclass(frozen=True, slots=True)
class LeadContext:
    """Everything a resolver may need. Read-only — a resolver must never
    mutate the context it's handed.
    """

    company_id: UUID
    company_name: str
    domain: str | None
    website: str | None
    country_code: str
    person_name: str | None = None
    known_facts: Mapping[str, str] = field(default_factory=dict)


@runtime_checkable
class Resolver(Protocol):
    name: str
    field: str
    tier: Tier
    cost_per_call: Decimal
    jurisdictions: frozenset[str] | None  # None = worldwide

    async def applicable(self, ctx: LeadContext) -> bool: ...
    async def resolve(self, ctx: LeadContext) -> list[Candidate]: ...


class BaseResolver(ABC):
    """Sane defaults so concrete resolvers stay short: a resolver only
    needs to set its class attributes and implement resolve().
    """

    name: str
    field: str
    tier: Tier
    cost_per_call: Decimal = Decimal("0")
    jurisdictions: frozenset[str] | None = None

    async def applicable(self, ctx: LeadContext) -> bool:
        """True when this lead's jurisdiction is one this resolver serves.
        Concrete resolvers override this for dynamic checks too (e.g. "only
        if we already know the domain") — the executor treats a False here
        as SKIPPED, not an error.
        """
        if self.jurisdictions is None:
            return True
        return ctx.country_code in self.jurisdictions

    @abstractmethod
    async def resolve(self, ctx: LeadContext) -> list[Candidate]: ...
