"""SuppressionRepository — addresses and domains that must never be contacted.

`is_suppressed` checks the address *and* its domain in one query, because
a domain-level suppression has to cover every address under it. The send
path calls this immediately before the SMTP handshake (Session 15's
second checkpoint), so a suppression added mid-campaign takes effect on
the very next message rather than at the next run.

`add` is idempotent: suppressing an already-suppressed address is a no-op,
not an IntegrityError. Someone unsubscribing twice is normal behaviour and
must never surface as an error.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.suppression import Suppression


class SuppressionCreate(BaseModel):
    model_config = ConfigDict(frozen=True)

    value: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class SuppressionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, suppression: SuppressionCreate) -> None:
        """Idempotent. Unsubscribing twice is not an error."""
        stmt = (
            pg_insert(Suppression)
            .values(
                value=self._normalise(suppression.value),
                reason=suppression.reason,
                added_at=datetime.now(UTC),
            )
            .on_conflict_do_nothing(index_elements=[Suppression.value])
        )
        await self._session.execute(stmt)

    async def is_suppressed(self, address: str) -> bool:
        """True if this address, or the whole domain it belongs to, is
        suppressed.
        """
        normalised = self._normalise(address)
        candidates = [normalised]
        _, _, domain = normalised.partition("@")
        if domain:
            candidates.append(domain)

        stmt = select(Suppression.id).where(Suppression.value.in_(candidates)).limit(1)
        return (await self._session.execute(stmt)).scalars().first() is not None

    async def filter_allowed(self, addresses: list[str]) -> list[str]:
        """Batch form for the send planner: one query for a whole run
        rather than one per recipient.
        """
        if not addresses:
            return []

        normalised = [self._normalise(a) for a in addresses]
        domains = {d for a in normalised if (d := a.partition("@")[2])}

        stmt = select(Suppression.value).where(
            or_(Suppression.value.in_(normalised), Suppression.value.in_(domains))
        )
        blocked = set((await self._session.execute(stmt)).scalars().all())

        return [
            original
            for original, norm in zip(addresses, normalised, strict=True)
            if norm not in blocked and norm.partition("@")[2] not in blocked
        ]

    async def list(self, *, limit: int = 200, offset: int = 0) -> list[Suppression]:
        stmt = (
            select(Suppression)
            .order_by(Suppression.added_at.desc(), Suppression.id.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def count(self) -> int:
        stmt = select(func.count()).select_from(Suppression)
        return int((await self._session.execute(stmt)).scalar_one())

    async def remove(self, suppression_id: uuid.UUID) -> None:
        row = await self._session.get(Suppression, suppression_id)
        if row is None:
            raise ValueError(f"Suppression {suppression_id} does not exist")
        await self._session.delete(row)
        await self._session.flush()

    @staticmethod
    def _normalise(value: str) -> str:
        """Email addresses and domains are case-insensitive. Storing them
        as typed would let 'Foo@Bar.com' slip past a suppression on
        'foo@bar.com' -- a compliance failure, not a cosmetic one.
        """
        return value.strip().lower()


__all__ = ["SuppressionCreate", "SuppressionRepository"]
