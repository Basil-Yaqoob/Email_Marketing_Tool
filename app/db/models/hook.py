"""Hook model — stub. Behaviour lands in Session 12 (hook mining agent).

Columns only, so Session 12 doesn't need a schema migration to add what it
already knows it needs: the mined hook text, always paired with a source
URL (CLAUDE.md rule 2.2 — a claim without a source is dropped, not stored).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import BaseModel


class Hook(BaseModel):
    __tablename__ = "hooks"

    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    hook_text: Mapped[str] = mapped_column(String, nullable=False)
    source_url: Mapped[str] = mapped_column(String, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    found_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
