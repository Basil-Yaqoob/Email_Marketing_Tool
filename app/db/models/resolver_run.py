"""The ResolverRun model — telemetry for every waterfall attempt.

This table is what makes the prototype's failure mode structurally
impossible: 1,640 consecutive verification errors were never recorded
anywhere, so nothing could ever have shown a yield collapse. Every
resolver call — hit, miss, error, or skipped — writes one row here.
Session 19's dashboard reads this table directly.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Integer, Numeric, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import BaseModel
from app.db.models.enums import ResolverOutcome


class ResolverRun(BaseModel):
    __tablename__ = "resolver_runs"

    resolver: Mapped[str] = mapped_column(String, nullable=False, index=True)
    field: Mapped[str] = mapped_column(String, nullable=False)
    # Not a hard FK — a resolver run may target a company, person, or email
    # row depending on the field, and may be null for discovery-stage runs
    # that don't yet have a subject (e.g. searching for companies).
    subject_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, index=True)
    tier: Mapped[int] = mapped_column(Integer, nullable=False)  # Tier enum's int value
    outcome: Mapped[ResolverOutcome] = mapped_column(
        SAEnum(ResolverOutcome, name="resolver_outcome"), nullable=False
    )
    candidates_returned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    cost: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False, default=Decimal(0))
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    ran_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
