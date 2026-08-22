"""The Campaign model — the root aggregate everything else hangs off."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Enum as SAEnum
from sqlalchemy import String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import BaseModel
from app.db.models.enums import CampaignStatus


class Campaign(BaseModel):
    __tablename__ = "campaigns"

    name: Mapped[str] = mapped_column(String, nullable=False)
    # ICP targeting rules: categories, locations, filters (Session 05+).
    icp: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # What this campaign is selling and who is signing it -- the shape of
    # app/agents/copy/offer.py's OfferConfig. Per campaign rather than
    # global: one user legitimately runs different offers to different
    # audiences, and the copywriter's whole output hangs off this.
    #
    # Empty until the user fills it in. The copy stage refuses to run on an
    # empty offer rather than writing an email signed by nobody about
    # nothing (offer.sender_ready() already warns about exactly this).
    offer: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # Default jurisdiction for leads that don't resolve one of their own
    # (Session 15). Null until the campaign wizard sets it.
    jurisdiction: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[CampaignStatus] = mapped_column(
        SAEnum(CampaignStatus, name="campaign_status"),
        nullable=False,
        default=CampaignStatus.DRAFT,
    )
