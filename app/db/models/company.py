"""The Company model.

country_code is NOT NULL deliberately: the policy engine (Session 15) picks
jurisdiction rules off it, and a null here is a compliance bug waiting to
happen — a lead with no known country could be silently sent under the wrong
jurisdiction's rules.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import BaseModel


class Company(BaseModel):
    __tablename__ = "companies"

    campaign_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    domain: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    website: Mapped[str | None] = mapped_column(String, nullable=True)
    address: Mapped[str | None] = mapped_column(String, nullable=True)
    # ISO-3166-1 alpha-2. Required — see module docstring.
    country_code: Mapped[str] = mapped_column(String(2), nullable=False)
    lat: Mapped[float | None] = mapped_column(nullable=True)
    lng: Mapped[float | None] = mapped_column(nullable=True)
    category: Mapped[str | None] = mapped_column(String, nullable=True)
    # Dedup key from discovery resolvers (Session 05) — a Google Place ID or
    # equivalent. Unique so the same physical business isn't inserted twice
    # by two different discovery runs.
    source_place_id: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    rating: Mapped[float | None] = mapped_column(nullable=True)
    review_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Full raw payload from the discovery source, kept for re-parsing without
    # a re-fetch if a later session needs a field we didn't extract yet.
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
