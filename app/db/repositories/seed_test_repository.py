"""SeedTestRepository — inbox-placement results from seed mailboxes.

A seed test answers the question deliverability actually turns on: not
"did the message send" but "where did it land". A campaign can report 100%
sent and 0% replies purely because every message went to spam, and nothing
else in the funnel can tell those two apart.

`latest_per_provider` is what the UI renders: placement is only meaningful
as the most recent reading per provider, since an old Gmail INBOX result
says nothing about today's reputation.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.enums import SeedPlacement
from app.db.models.seed_test import SeedTest


class SeedTestCreate(BaseModel):
    model_config = ConfigDict(frozen=True)

    campaign_id: uuid.UUID
    provider: str = Field(min_length=1)
    mailbox_address: str = Field(min_length=3)
    placement: SeedPlacement = SeedPlacement.UNKNOWN
    tested_at: datetime | None = None


class SeedTestRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, test: SeedTestCreate) -> SeedTest:
        payload = test.model_dump()
        payload["tested_at"] = test.tested_at or datetime.now(UTC)
        row = SeedTest(**payload)
        self._session.add(row)
        await self._session.flush()
        return row

    async def latest_per_provider(self, campaign_id: uuid.UUID) -> dict[str, SeedPlacement]:
        """Most recent placement per provider.

        An older reading is not averaged in: reputation changes, and the
        mean of a good week and a bad day describes neither.
        """
        newest = (
            select(
                SeedTest.provider,
                func.max(SeedTest.tested_at).label("tested_at"),
            )
            .where(SeedTest.campaign_id == campaign_id)
            .group_by(SeedTest.provider)
            .subquery()
        )
        stmt = (
            select(SeedTest)
            .join(
                newest,
                (SeedTest.provider == newest.c.provider)
                & (SeedTest.tested_at == newest.c.tested_at),
            )
            .where(SeedTest.campaign_id == campaign_id)
        )

        result = await self._session.execute(stmt)
        return {row.provider: row.placement for row in result.scalars().all()}

    async def history(self, campaign_id: uuid.UUID, *, limit: int = 100) -> list[SeedTest]:
        stmt = (
            select(SeedTest)
            .where(SeedTest.campaign_id == campaign_id)
            .order_by(SeedTest.tested_at.desc(), SeedTest.id.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def spam_rate(self, campaign_id: uuid.UUID) -> float:
        """Share of the most recent readings that landed in spam.

        Returns 0.0 with no readings -- 'never tested' is not 'perfect
        placement', and the UI must say which it is rather than showing a
        reassuring zero.
        """
        latest = await self.latest_per_provider(campaign_id)
        if not latest:
            return 0.0
        in_spam = sum(1 for placement in latest.values() if placement == SeedPlacement.SPAM)
        return in_spam / len(latest)


__all__ = ["SeedTestCreate", "SeedTestRepository"]
