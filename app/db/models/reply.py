"""Reply model — stub. Behaviour lands in Session 17 (IMAP ingestion) and
Session 18 (classification & actions).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import BaseModel
from app.db.models.enums import ReplyClassification


class Reply(BaseModel):
    __tablename__ = "replies"

    send_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sends.id", ondelete="CASCADE"), nullable=False, index=True
    )
    classification: Mapped[ReplyClassification | None] = mapped_column(
        SAEnum(ReplyClassification, name="reply_classification"), nullable=True
    )
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_snippet: Mapped[str] = mapped_column(String, nullable=False)
