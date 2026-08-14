"""Message model — stub. Behaviour lands in Session 13 (copywriter) and
Session 21 (review queue UI).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import BaseModel
from app.db.models.enums import MessageStatus


class Message(BaseModel):
    __tablename__ = "messages"

    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    person_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("people.id", ondelete="SET NULL"), nullable=True
    )
    angle: Mapped[str] = mapped_column(String, nullable=False)
    subject: Mapped[str] = mapped_column(String, nullable=False)
    body: Mapped[str] = mapped_column(String, nullable=False)
    # Mechanical scan findings, critic score, revision history, quote
    # verdict — everything Session 13 records so review (Session 21) never
    # happens blind.
    quality_report: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[MessageStatus] = mapped_column(
        SAEnum(MessageStatus, name="message_status"),
        nullable=False,
        default=MessageStatus.DRAFT,
    )
