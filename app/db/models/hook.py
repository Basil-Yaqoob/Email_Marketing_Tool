"""Hook model — a mined, sourced fact about a company, or the record of
why none was found (Session 12).

A claim without a source URL is dropped, not stored (CLAUDE.md rule 2.2)
— source_url is nullable only because a NONE_FOUND row legitimately has
none; a FOUND row without one never reaches this table at all, since the
validator (app/agents/hooks/validation.py) demotes it to NONE_FOUND
first.

`notes` is what makes a blank trustworthy rather than indistinguishable
from a silent failure — it always carries the reason, ported directly
from the original agent's `rejection_reason`/`review_note` fields.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import BaseModel
from app.db.models.enums import HookChannel, HookConfidenceLevel, HookNewsType, HookVerdict


class Hook(BaseModel):
    __tablename__ = "hooks"

    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[HookVerdict] = mapped_column(
        SAEnum(HookVerdict, name="hook_verdict"), nullable=False
    )
    # Nullable together: a NONE_FOUND row has neither, by construction --
    # see this module's docstring.
    hook_text: Mapped[str | None] = mapped_column(String, nullable=True)
    source_url: Mapped[str | None] = mapped_column(String, nullable=True)
    event_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    news_type: Mapped[HookNewsType] = mapped_column(
        SAEnum(HookNewsType, name="hook_news_type"), nullable=False, default=HookNewsType.NONE
    )
    channel: Mapped[HookChannel] = mapped_column(
        SAEnum(HookChannel, name="hook_channel"), nullable=False, default=HookChannel.NONE
    )
    # The research model's own self-rating (high/medium/low), not the
    # numeric 0-1 scale Fact.confidence uses elsewhere in this schema.
    confidence: Mapped[HookConfidenceLevel] = mapped_column(
        SAEnum(HookConfidenceLevel, name="hook_confidence_level"),
        nullable=False,
        default=HookConfidenceLevel.LOW,
    )
    swap_test_passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    needs_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Always populated: the rejection reason for a blank, or a review note
    # for a found-but-flagged hook. Never left empty — see module docstring.
    notes: Mapped[str] = mapped_column(String, nullable=False, default="")
    # Full research transcript, kept for auditing why a call landed where
    # it did — the original saved these to out/transcripts/ per lead.
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    found_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
