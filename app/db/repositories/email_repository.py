"""EmailAddressRepository — candidate addresses and their verification state.

`pending_verification` is what the verification stage pulls from, and it
deliberately returns only UNCHECKED rows. UNKNOWN is *not* re-queued: an
address that came back UNKNOWN is not unverified, it is verified-as-
unverifiable -- the domain is Google Workspace, Microsoft 365, or a
catch-all, and no amount of re-probing will ever resolve it (CLAUDE.md
§10). Re-queuing those would burn SMTP probes forever on addresses whose
answer cannot change, and would make the funnel look like verification
was failing when it was working exactly as designed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.company import Company
from app.db.models.email import EmailAddress
from app.db.models.enums import VerifyStatus

# Statuses that mean "the ladder has run and reached a conclusion", even
# when that conclusion is 'cannot be determined'.
_TERMINAL = (
    VerifyStatus.VALID,
    VerifyStatus.INVALID,
    VerifyStatus.CATCH_ALL,
    VerifyStatus.UNKNOWN,
    VerifyStatus.ROLE,
)


class EmailAddressCreate(BaseModel):
    model_config = ConfigDict(frozen=True)

    company_id: uuid.UUID
    address: str = Field(min_length=3)
    person_id: uuid.UUID | None = None
    pattern_used: str | None = None
    verify_status: VerifyStatus = VerifyStatus.UNCHECKED
    is_role_account: bool = False


class EmailAddressRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, email: EmailAddressCreate) -> EmailAddress:
        row = EmailAddress(**email.model_dump())
        self._session.add(row)
        await self._session.flush()
        return row

    async def bulk_add(self, emails: list[EmailAddressCreate]) -> list[EmailAddress]:
        if not emails:
            return []
        rows = [EmailAddress(**e.model_dump()) for e in emails]
        self._session.add_all(rows)
        await self._session.flush()
        return rows

    async def get(self, email_id: uuid.UUID) -> EmailAddress | None:
        return await self._session.get(EmailAddress, email_id)

    async def for_company(self, company_id: uuid.UUID) -> list[EmailAddress]:
        stmt = (
            select(EmailAddress)
            .where(EmailAddress.company_id == company_id)
            .order_by(EmailAddress.created_at, EmailAddress.id)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def pending_verification(
        self, campaign_id: uuid.UUID, *, limit: int = 500
    ) -> list[EmailAddress]:
        """Addresses the verification ladder has not run against yet.

        Only UNCHECKED — see the module docstring on why UNKNOWN is never
        re-queued.
        """
        stmt = (
            select(EmailAddress)
            .join(Company, EmailAddress.company_id == Company.id)
            .where(
                Company.campaign_id == campaign_id,
                EmailAddress.verify_status == VerifyStatus.UNCHECKED,
            )
            .order_by(EmailAddress.created_at, EmailAddress.id)
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def set_verify_status(
        self,
        email_id: uuid.UUID,
        status: VerifyStatus,
        *,
        verified_at: datetime | None = None,
    ) -> EmailAddress:
        row = await self._session.get(EmailAddress, email_id)
        if row is None:
            raise ValueError(f"EmailAddress {email_id} does not exist")
        row.verify_status = status
        if status in _TERMINAL:
            row.verified_at = verified_at or datetime.now(UTC)
        await self._session.flush()
        return row

    async def status_breakdown(self, campaign_id: uuid.UUID) -> dict[str, int]:
        """Counts per verification status, for the leads-table filter chips
        and the honest 'why is this UNKNOWN' explanation beside them.
        """
        stmt = (
            select(EmailAddress.verify_status, func.count())
            .join(Company, EmailAddress.company_id == Company.id)
            .where(Company.campaign_id == campaign_id)
            .group_by(EmailAddress.verify_status)
        )
        result = await self._session.execute(stmt)
        return {str(status.value): count for status, count in result.all()}


__all__ = ["EmailAddressCreate", "EmailAddressRepository"]
