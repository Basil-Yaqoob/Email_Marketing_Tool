"""Send model — stub. Behaviour lands in Session 16 (sending engine)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import BaseModel
from app.db.models.enums import SendStatus


class Send(BaseModel):
    __tablename__ = "sends"

    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    mailbox_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("mailboxes.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[SendStatus] = mapped_column(
        SAEnum(SendStatus, name="send_status"), nullable=False, default=SendStatus.QUEUED
    )
    # RFC Message-ID header, needed for thread matching (Session 17) on
    # In-Reply-To / References when a reply comes back.
    message_id_header: Mapped[str | None] = mapped_column(String, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
