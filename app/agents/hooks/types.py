"""Shared types for the hook-mining agent.

`HookRecord` mirrors recent_news_agent's `VERDICT_SCHEMA` exactly — every
field required, no nullables, no additionalProperties, so it satisfies
strict JSON-schema mode on every provider. `HookResult` is what
validation.py produces after applying the evidence bar in code; it is the
only shape that ever reaches persistence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from pydantic import BaseModel, Field

from app.db.models.enums import HookChannel, HookConfidenceLevel, HookNewsType, HookVerdict


@dataclass(frozen=True, slots=True)
class HookMiningLead:
    """What the agent needs to know about one company. Not a SQLAlchemy
    model — persistence models don't cross layers (CLAUDE.md).
    """

    company_name: str
    website: str | None = None
    city: str | None = None
    industry: str | None = None  # e.g. "dental clinic" — drives HEALTHCARE-only prompt notes
    primary_wedge: str | None = None  # the email angle to bridge the hook to
    instagram_handle: str | None = None
    notes: str | None = None  # operator note; treated as evidence, never instruction
    brief_note: str | None = None  # sheet-specific guidance, e.g. a job-advert lead source


class HookRecord(BaseModel):
    """The transcriber's strict-JSON output. Field names and enum values
    match the original schema verbatim — this is a port, not a redesign.
    """

    verdict: HookVerdict
    hook: str
    source_url: str
    date: str  # "YYYY-MM-DD" | "YYYY-MM" | "YYYY" | "" -- free text, parsed later
    news_type: HookNewsType
    channel: HookChannel
    evidence: str
    confidence: HookConfidenceLevel
    rejection_reason: str = Field(description="Required when verdict is none_found")
    needs_review: bool
    review_note: str


@dataclass(frozen=True, slots=True)
class HookResult:
    """The validated, code-enforced outcome. This is the only shape that
    ever reaches the `hooks` table.
    """

    status: HookVerdict
    hook_text: str | None
    source_url: str | None
    event_date: date | None
    news_type: HookNewsType
    channel: HookChannel
    confidence: HookConfidenceLevel
    swap_test_passed: bool
    needs_review: bool
    # Always populated -- the reason for a blank, or a caveat for a found
    # hook. A silent blank is indistinguishable from a failure, which is
    # the exact failure mode this whole product exists to avoid.
    notes: str
    transcript: str = ""
    model: str = ""
    cost_usd: float = 0.0

    @property
    def is_found(self) -> bool:
        return self.status == HookVerdict.FOUND
