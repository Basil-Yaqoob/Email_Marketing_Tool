"""SendRepository — one row per delivery attempt, plus the daily counters
the throttle depends on.

`sent_today` is the load-bearing method: `app/sending/scheduler.py` caps a
mailbox at `daily_cap` sends per day, and that cap is only real if the
count it checks against comes from the database rather than from an
in-process counter that resets whenever a worker restarts.

Warmup traffic is deliberately *not* modelled here yet. Phase 6's warmup
pool needs its own accounting, because warming a mailbox must never
consume the campaign quota it is being warmed to earn -- counting both
through this table would make a warming mailbox appear to have spent its
daily cap before sending a single real message.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.enums import SendStatus
from app.db.models.send import Send

# Statuses that consumed a real delivery slot. A FAILED send never left the
# mailbox, so it must not count against the daily cap -- otherwise a run of
# transient SMTP errors would silently throttle a healthy mailbox to zero.
_CONSUMED_A_SLOT = (SendStatus.SENT, SendStatus.BOUNCED)


class SendCreate(BaseModel):
    model_config = ConfigDict(frozen=True)

    message_id: uuid.UUID
    mailbox_id: uuid.UUID
    status: SendStatus = SendStatus.QUEUED
    message_id_header: str | None = None


class SendRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, send: SendCreate) -> Send:
        row = Send(**send.model_dump())
        self._session.add(row)
        await self._session.flush()
        return row

    async def get(self, send_id: uuid.UUID) -> Send | None:
        return await self._session.get(Send, send_id)

    async def mark_sent(
        self, send_id: uuid.UUID, *, message_id_header: str, sent_at: datetime | None = None
    ) -> Send:
        row = await self._require(send_id)
        row.status = SendStatus.SENT
        row.message_id_header = message_id_header
        row.sent_at = sent_at or datetime.now(UTC)
        await self._session.flush()
        return row

    async def mark_failed(self, send_id: uuid.UUID, status: SendStatus) -> Send:
        if status not in (SendStatus.FAILED, SendStatus.BOUNCED):
            raise ValueError(f"{status} is not a failure status")
        row = await self._require(send_id)
        row.status = status
        await self._session.flush()
        return row

    async def sent_today(self, mailbox_id: uuid.UUID, *, day: date | None = None) -> int:
        """How many sends this mailbox has already spent today.

        Counted from the database, not memory: the throttle is a real cap
        on a real domain's reputation, and a counter that resets on worker
        restart is not a cap at all.
        """
        target = day or datetime.now(UTC).date()
        start = datetime.combine(target, time.min, tzinfo=UTC)
        end = datetime.combine(target, time.max, tzinfo=UTC)

        stmt = (
            select(func.count())
            .select_from(Send)
            .where(
                Send.mailbox_id == mailbox_id,
                Send.status.in_(_CONSUMED_A_SLOT),
                Send.sent_at.between(start, end),
            )
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def by_message_id_header(self, header: str) -> Send | None:
        """Find the send a reply belongs to. Session 17's IMAP matcher
        threads an inbound reply back to its original send by this header.
        """
        stmt = select(Send).where(Send.message_id_header == header).limit(1)
        return (await self._session.execute(stmt)).scalars().first()

    async def _require(self, send_id: uuid.UUID) -> Send:
        row = await self._session.get(Send, send_id)
        if row is None:
            raise ValueError(f"Send {send_id} does not exist")
        return row


__all__ = ["SendCreate", "SendRepository"]
