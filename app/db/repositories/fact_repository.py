"""FactRepository — the only way anything writes to the facts table.

CLAUDE.md rule 2.2: no value enters the database as a bare string, and
corrections never UPDATE a row — they supersede it. This module is the
enforcement point for both rules.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.enums import SubjectType
from app.db.models.fact import Fact


class FactCreate(BaseModel):
    """Boundary type for writing a fact. Never pass a bare dict or a
    SQLAlchemy model across a layer — CLAUDE.md §6.
    """

    model_config = ConfigDict(frozen=True)

    subject_type: SubjectType
    subject_id: uuid.UUID
    field: str
    value: str
    confidence: float = Field(ge=0.0, le=1.0)
    source: str
    source_url: str | None = None
    retrieved_at: datetime
    independence_key: str | None = None


class FactRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, fact: FactCreate) -> Fact:
        """Insert a new fact row. Never updates an existing row — a
        correction goes through supersede() instead, so the full history of
        what the system believed is preserved.
        """
        row = Fact(**fact.model_dump())
        self._session.add(row)
        await self._session.flush()
        return row

    async def supersede(self, old_id: uuid.UUID, new: FactCreate) -> Fact:
        """Insert the new fact, then point the old fact's superseded_by at
        it — both against the same flush, so a caller that awaits this once
        never observes a half-superseded state.
        """
        old_row = await self._session.get(Fact, old_id)
        if old_row is None:
            raise ValueError(f"Fact {old_id} does not exist; cannot supersede")

        new_row = Fact(**new.model_dump())
        self._session.add(new_row)
        await self._session.flush()  # assigns new_row.id

        old_row.superseded_by = new_row.id
        await self._session.flush()
        return new_row

    async def current(self, subject_id: uuid.UUID, field: str) -> list[Fact]:
        """Facts for (subject_id, field) that have not been superseded,
        best confidence first — what a copywriter or resolver should read.
        """
        stmt = (
            select(Fact)
            .where(
                Fact.subject_id == subject_id,
                Fact.field == field,
                Fact.superseded_by.is_(None),
            )
            .order_by(Fact.confidence.desc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())
