"""Declarative base and shared id/created_at mixin for all models.

UUID primary keys, not integers: an integer PK leaks record volume (a
competitor watching your API can infer lead count from the id) and makes
merging data across campaigns painful. See doc/plans/session-02-data-model.md.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Shared declarative base. Import this, never sqlalchemy.orm directly,
    so every model registers on the same metadata that Alembic reads.
    """


class BaseModel(Base):
    """Abstract base giving every concrete model a UUID id and created_at.

    All timestamps are timezone=True and stored in UTC — the sending engine
    (Session 16) needs to convert to the recipient's local time later, and
    storing naive/local time now would corrupt that conversion irrecoverably.
    """

    __abstract__ = True

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
