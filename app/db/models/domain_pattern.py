"""The DomainPattern model — global, not scoped to any campaign.

This is the compounding asset described in CLAUDE.md and the Session 09
plan: one confirmed address on a domain tells you the format for every
other person at that domain, turning ~9 blind pattern guesses into 1
targeted check. Scoping it per-campaign would throw that away every time a
new campaign touches a domain the system has already learned.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import BaseModel


class DomainPattern(BaseModel):
    __tablename__ = "domain_patterns"

    domain: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)
    # "{first}.{last}", "{f}{last}", ...
    pattern: Mapped[str] = mapped_column(String, nullable=False)
    confirmed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
