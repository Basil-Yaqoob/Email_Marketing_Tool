"""CompanyRepository — discovery's write path and the lead list's read path.

`bulk_upsert` is the important one. Discovery resolvers overlap by design
(OSM and Google Places both know the same dentist), and re-running
discovery on a campaign must be idempotent rather than duplicating every
company. Dedup happens in the database via ON CONFLICT DO NOTHING against
a unique key, not by SELECT-then-INSERT in Python, which would race
between concurrent workers.

Rows whose `source_place_id` is NULL never conflict (NULL != NULL in SQL),
so a company from a source that has no stable id is always inserted --
correct, because there is nothing to dedup it against. Session 05's
`app/resolvers/discovery/dedup.py` handles that fuzzier case (name + geo
proximity) before rows ever reach here.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.company import Company

# Matches the constraint created in migration 7ed091f9c1fc. Named so the
# ON CONFLICT target is explicit rather than inferred from column order.
_PLACE_UNIQUE = "uq_companies_campaign_place"


class CompanyCreate(BaseModel):
    model_config = ConfigDict(frozen=True)

    campaign_id: uuid.UUID
    name: str = Field(min_length=1)
    country_code: str = Field(min_length=2, max_length=2)
    domain: str | None = None
    website: str | None = None
    address: str | None = None
    lat: float | None = None
    lng: float | None = None
    category: str | None = None
    source_place_id: str | None = None
    rating: float | None = None
    review_count: int | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class UpsertResult(BaseModel):
    """What a discovery run actually changed. `skipped` is a normal,
    expected outcome on a re-run, not an error -- the UI says "already
    known", never "failed".
    """

    model_config = ConfigDict(frozen=True)

    inserted: int = 0
    skipped: int = 0

    @property
    def submitted(self) -> int:
        return self.inserted + self.skipped


class CompanyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def bulk_upsert(self, companies: list[CompanyCreate]) -> UpsertResult:
        """Insert many companies, skipping ones this campaign already has.

        Returns counts rather than rows: a discovery run inserting 400
        companies has no reason to hydrate 400 ORM objects the caller will
        not read.
        """
        if not companies:
            return UpsertResult()

        values = [c.model_dump() for c in companies]
        stmt = (
            pg_insert(Company)
            .values(values)
            .on_conflict_do_nothing(constraint=_PLACE_UNIQUE)
            .returning(Company.id)
        )
        result = await self._session.execute(stmt)
        inserted = len(list(result.scalars().all()))
        return UpsertResult(inserted=inserted, skipped=len(values) - inserted)

    async def get(self, company_id: uuid.UUID) -> Company | None:
        return await self._session.get(Company, company_id)

    async def count(self, campaign_id: uuid.UUID) -> int:
        stmt = select(func.count()).select_from(Company).where(Company.campaign_id == campaign_id)
        return int((await self._session.execute(stmt)).scalar_one())

    async def list_for_campaign(
        self,
        campaign_id: uuid.UUID,
        *,
        limit: int = 50,
        offset: int = 0,
        has_website: bool | None = None,
    ) -> list[Company]:
        stmt = select(Company).where(Company.campaign_id == campaign_id)
        if has_website is True:
            stmt = stmt.where(Company.website.isnot(None))
        elif has_website is False:
            stmt = stmt.where(Company.website.is_(None))
        # Tie-broken by id: bulk_upsert writes every company in one
        # transaction, so they all share a `created_at` (Postgres now() is
        # transaction time). Ordering on the timestamp alone would make
        # LIMIT/OFFSET non-deterministic and let the leads table repeat a
        # row on page 2 while dropping another entirely.
        stmt = (
            stmt.order_by(Company.created_at.desc(), Company.id.desc()).limit(limit).offset(offset)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def with_website(self, campaign_id: uuid.UUID, *, limit: int = 500) -> list[Company]:
        """Companies the enrichment stage can actually crawl. A company
        with no website is not a failure to enrich -- there is simply
        nothing to fetch, so it is never handed to the crawler at all.
        """
        stmt = (
            select(Company)
            .where(Company.campaign_id == campaign_id, Company.website.isnot(None))
            .order_by(Company.created_at, Company.id)
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())


__all__ = ["CompanyCreate", "CompanyRepository", "UpsertResult"]
