"""Suppression model — stub. Behaviour lands in Session 15 (policy &
compliance engine).

Global by design, same reasoning as domain_patterns: an unsubscribe or hard
bounce must suppress across every campaign, not just the one that triggered
it, or the product re-emails someone who opted out.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import BaseModel


class Suppression(BaseModel):
    __tablename__ = "suppressions"

    # An email address ("jane@acme.com") or a bare domain ("acme.com") —
    # scope is inferred by whether value contains "@", enforced in the
    # repository layer (Session 15), not here.
    value: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    reason: Mapped[str] = mapped_column(String, nullable=False)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
