"""SeedTest model — stub. Behaviour lands in Session 19 (analytics &
instrumentation): seed-list placement testing over IMAP.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import BaseModel
from app.db.models.enums import SeedPlacement


class SeedTest(BaseModel):
    __tablename__ = "seed_tests"

    campaign_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String, nullable=False)  # "gmail", "outlook", ...
    mailbox_address: Mapped[str] = mapped_column(String, nullable=False)
    placement: Mapped[SeedPlacement] = mapped_column(
        SAEnum(SeedPlacement, name="seed_placement"),
        nullable=False,
        default=SeedPlacement.UNKNOWN,
    )
    tested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
