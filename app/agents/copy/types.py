"""Shared types for the copywriting pipeline.

Mirrors `app/agents/hooks/types.py`'s shape: a plain dataclass for what the
agent needs to know about one lead (not a SQLAlchemy model — persistence
models don't cross layers, CLAUDE.md), Pydantic schemas for the two
strict-JSON legs of the pipeline (strategist, critic), and a plain
dataclass for the validated outcome that is the only shape reaching
persistence.

`QuoteVerdict` here and `QuoteClass` in `quote_classification.py` are
deliberately two different vocabularies, exactly as in the original. One is
the mechanical, keyword-driven read of a harvested quote
(`quote_classification.classify_quote`); the other is the strategist's own
*decision* about how to use it, which the model returns and which this
module's `StrategyRecord` captures verbatim from the original
`STRATEGY_SCHEMA`.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from app.db.models.enums import RoleClass


class QuoteVerdict(enum.StrEnum):
    USABLE_COMPLAINT = "usable_complaint"
    INVERTED_PRAISE = "inverted_praise"
    SCHEDULING_FRICTION = "scheduling_friction"
    DISCARDED_UNCLEAR = "discarded_unclear"
    NO_QUOTE = "no_quote"


class OpenerType(enum.StrEnum):
    NEWS_HOOK = "news_hook"
    REVIEW_QUOTE = "review_quote"
    COMPETITOR = "competitor"
    OBSERVABLE_WEDGE = "observable_wedge"


class RoleAngle(enum.StrEnum):
    REVENUE = "revenue"
    WORKLOAD = "workload"
    INTRO_PATH = "intro_path"


class PSService(enum.StrEnum):
    NONE = "none"
    AUTOMATION = "automation"
    WEB = "web"


class StrategyConfidence(enum.StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class AITellType(enum.StrEnum):
    PUNCTUATION = "punctuation"
    VOCABULARY = "vocabulary"
    STRUCTURE = "structure"
    REGISTER = "register"
    NONE = "none"


class SubjectVerdict(enum.StrEnum):
    GOOD = "good"
    REVEALS_PITCH = "reveals_pitch"
    TOO_LONG = "too_long"
    GENERIC = "generic"
    TEMPLATE_SHAPE = "template_shape"


class ReplyLikelihood(enum.StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class StrategyRecord(BaseModel):
    """The strategist's strict-JSON output. Field names and enum values
    match the original `STRATEGY_SCHEMA` verbatim — this is a port, not a
    redesign.
    """

    opener_type: OpenerType
    subject: str
    opener_observation: str
    opener_source: str
    quote_verdict: QuoteVerdict
    quote_reasoning: str
    competitor_used: str
    subject_rationale: str
    problem_statement: str
    role_angle: RoleAngle
    value_points: list[str]
    cta: str
    ps_service: PSService
    ps_justification: str
    tone: str
    avoid: list[str]
    confidence: StrategyConfidence


class AITell(BaseModel):
    quote: str
    tell_type: AITellType
    fix: str


class CritiqueRecord(BaseModel):
    """The critic's strict-JSON output. Ported from `CRITIC_SCHEMA` verbatim."""

    score: int
    passes: bool
    ai_tells: list[AITell]
    subject_verdict: SubjectVerdict
    subject_fix: str
    swap_test_passed: bool
    swap_test_reasoning: str
    fabrications: list[str]
    reply_likelihood: ReplyLikelihood
    weakest_line: str
    required_fixes: list[str]
    verdict_summary: str


@dataclass(frozen=True, slots=True)
class CompetitorInfo:
    """One rival on file for a lead, strongest threat first.

    Category/geography alignment (does this rival actually compete for the
    same customer?) is the discovery layer's job — a resolver that does not
    exist yet, see the roadmap's Discovered work — not this agent's. By the
    time a `CompetitorInfo` reaches here it is assumed already usable.
    """

    name: str
    threat: float = 0.0
    rating: str = ""
    reviews: str = ""
    why: str = ""


@dataclass(frozen=True, slots=True)
class CopyLead:
    """What the copywriting pipeline needs to know about one company. Not a
    SQLAlchemy model — persistence models don't cross layers (CLAUDE.md).

    `hook_text`/`hook_source_url`/`hook_date` come from a FOUND `Hook` row
    (Session 12); a hook without a source URL is never eligible for the
    `news_hook` angle regardless of `hook_text` — see
    `dossier.eligible_angles` (CLAUDE.md rule 2.2, same provenance bar the
    hook validator itself enforces before a hook ever reaches this shape).
    """

    company_name: str
    contact_name: str | None = None
    contact_title: str | None = None
    role_class: RoleClass = RoleClass.OTHER
    website: str | None = None
    city: str | None = None
    industry: str | None = None
    rating: str | None = None
    review_count: str | None = None
    missed_call_evidence: str | None = None
    review_themes: str | None = None
    hook_text: str | None = None
    hook_source_url: str | None = None
    hook_date: str | None = None
    competitors: tuple[CompetitorInfo, ...] = ()
    primary_wedge: str | None = None
    after_hours_gap: bool | None = None
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class MessageResult:
    """The validated outcome of one angle's email. This is the only shape
    that ever reaches the `messages` table (`MessageRepository`).
    """

    angle: str
    subject: str
    body: str
    passed: bool
    score: int
    strategy: dict[str, Any]
    critique: dict[str, Any]
    scan: dict[str, Any]
    attempts: list[dict[str, Any]] = field(default_factory=list)
    model: str = ""
    cost_usd: float = 0.0


__all__ = [
    "AITell",
    "AITellType",
    "CompetitorInfo",
    "CopyLead",
    "CritiqueRecord",
    "MessageResult",
    "OpenerType",
    "PSService",
    "QuoteVerdict",
    "ReplyLikelihood",
    "RoleAngle",
    "StrategyConfidence",
    "StrategyRecord",
    "SubjectVerdict",
]
