"""The EmailAddress model and its verification status.

See app/db/models/enums.py:VerifyStatus for why UNKNOWN is a first-class,
permanent outcome rather than an error state.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import BaseModel
from app.db.models.enums import VerifyStatus


class EmailAddress(BaseModel):
    __tablename__ = "email_addresses"

    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Null when the address is a role account (info@, sales@) with no known
    # named owner, or when a pattern guess hasn't been tied to a person yet.
    person_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("people.id", ondelete="SET NULL"), nullable=True, index=True
    )
    address: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # The domain_patterns format string that produced this guess, if any
    # (e.g. "{first}.{last}") — null for addresses found directly on a page.
    pattern_used: Mapped[str | None] = mapped_column(String, nullable=True)
    verify_status: Mapped[VerifyStatus] = mapped_column(
        SAEnum(VerifyStatus, name="verify_status"),
        nullable=False,
        default=VerifyStatus.UNCHECKED,
    )
    verified_at: Mapped[datetime | None] = mapped_column(nullable=True)
    is_role_account: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
