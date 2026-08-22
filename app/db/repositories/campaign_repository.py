"""CampaignRepository — campaigns and the funnel numbers hung off them.

The funnel is the number a user actually looks at, so `funnel()` computes
every stage in one round trip via correlated subqueries rather than seven
separate COUNTs. A dashboard listing N campaigns would otherwise issue 7N
queries.

Counts are deliberately *company*-shaped, not row-shaped, for the middle
stages: `person_found` is "companies that have at least one person", not
"how many people exist". A single company with four contacts is one
company that converted, and counting rows there would quietly inflate the
funnel past 100% of what was discovered.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.campaign import Campaign
from app.db.models.company import Company
from app.db.models.email import EmailAddress
from app.db.models.enums import CampaignStatus, MessageStatus, SendStatus, VerifyStatus
from app.db.models.message import Message
from app.db.models.person import Person
from app.db.models.reply import Reply
from app.db.models.send import Send


class CampaignCreate(BaseModel):
    """Boundary type for creating a campaign — never a bare dict across a
    layer (CLAUDE.md §6).
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    icp: dict[str, Any] = Field(default_factory=dict)
    jurisdiction: str | None = None


class FunnelCounts(BaseModel):
    """One campaign's funnel. Every stage is a count of companies except
    `sent`/`replied`, which are counts of actual sends and replies.
    """

    model_config = ConfigDict(frozen=True)

    discovered: int = 0
    with_website: int = 0
    person_found: int = 0
    email_found: int = 0
    verified: int = 0
    drafted: int = 0
    sent: int = 0
    replied: int = 0


class CampaignRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, data: CampaignCreate) -> Campaign:
        row = Campaign(**data.model_dump())
        self._session.add(row)
        await self._session.flush()
        return row

    async def get(self, campaign_id: uuid.UUID) -> Campaign | None:
        return await self._session.get(Campaign, campaign_id)

    async def list(self) -> list[Campaign]:
        """Newest first — the campaign someone just created should be the
        one at the top of the dashboard.

        Tie-broken by id because `created_at` defaults to Postgres `now()`,
        which is *transaction* time: every row written in one transaction
        shares a timestamp. Ordering on it alone is non-deterministic, and
        a non-deterministic ORDER BY under LIMIT/OFFSET silently repeats
        and drops rows between pages.
        """
        stmt = select(Campaign).order_by(Campaign.created_at.desc(), Campaign.id.desc())
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def set_status(self, campaign_id: uuid.UUID, status: CampaignStatus) -> Campaign:
        row = await self._session.get(Campaign, campaign_id)
        if row is None:
            raise ValueError(f"Campaign {campaign_id} does not exist")
        row.status = status
        await self._session.flush()
        return row

    async def update(
        self,
        campaign_id: uuid.UUID,
        *,
        name: str | None = None,
        icp: dict[str, Any] | None = None,
        jurisdiction: str | None = None,
    ) -> Campaign:
        """Partial update. Fields left as None are untouched, so a caller
        editing only the name cannot accidentally blank the ICP.
        """
        row = await self._session.get(Campaign, campaign_id)
        if row is None:
            raise ValueError(f"Campaign {campaign_id} does not exist")
        if name is not None:
            row.name = name
        if icp is not None:
            row.icp = icp
        if jurisdiction is not None:
            row.jurisdiction = jurisdiction
        await self._session.flush()
        return row

    async def funnel(self, campaign_id: uuid.UUID) -> FunnelCounts:
        """Every funnel stage in one query."""
        companies = select(Company.id).where(Company.campaign_id == campaign_id).subquery()

        def _companies_matching(exists_stmt: Select[Any]) -> Any:
            """Count companies in this campaign for which the subquery has
            at least one row — EXISTS, not a JOIN, so a company with four
            people still counts once.
            """
            return select(func.count()).select_from(companies).where(exists_stmt.exists())

        stmt = select(
            select(func.count()).select_from(companies).scalar_subquery().label("discovered"),
            select(func.count())
            .select_from(Company)
            .where(Company.campaign_id == campaign_id, Company.website.isnot(None))
            .scalar_subquery()
            .label("with_website"),
            _companies_matching(select(Person.id).where(Person.company_id == companies.c.id))
            .scalar_subquery()
            .label("person_found"),
            _companies_matching(
                select(EmailAddress.id).where(EmailAddress.company_id == companies.c.id)
            )
            .scalar_subquery()
            .label("email_found"),
            _companies_matching(
                select(EmailAddress.id).where(
                    EmailAddress.company_id == companies.c.id,
                    EmailAddress.verify_status == VerifyStatus.VALID,
                )
            )
            .scalar_subquery()
            .label("verified"),
            _companies_matching(select(Message.id).where(Message.company_id == companies.c.id))
            .scalar_subquery()
            .label("drafted"),
            select(func.count())
            .select_from(Send)
            .join(Message, Send.message_id == Message.id)
            .join(Company, Message.company_id == Company.id)
            .where(Company.campaign_id == campaign_id, Send.status == SendStatus.SENT)
            .scalar_subquery()
            .label("sent"),
            select(func.count())
            .select_from(Reply)
            .join(Send, Reply.send_id == Send.id)
            .join(Message, Send.message_id == Message.id)
            .join(Company, Message.company_id == Company.id)
            .where(Company.campaign_id == campaign_id)
            .scalar_subquery()
            .label("replied"),
        )

        row = (await self._session.execute(stmt)).one()
        return FunnelCounts.model_validate(row._mapping)

    async def approved_message_count(self, campaign_id: uuid.UUID) -> int:
        """How many messages are cleared to send. `launch_campaign` needs
        this for its pre-send preview, which must never guess.
        """
        stmt = (
            select(func.count())
            .select_from(Message)
            .join(Company, Message.company_id == Company.id)
            .where(
                Company.campaign_id == campaign_id,
                Message.status == MessageStatus.APPROVED,
            )
        )
        return int((await self._session.execute(stmt)).scalar_one())


__all__ = ["CampaignCreate", "CampaignRepository", "FunnelCounts"]
