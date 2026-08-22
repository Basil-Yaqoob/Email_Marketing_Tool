"""ReplyRepository — inbound replies, threaded back to the send that
provoked them.

`reply_rate` is one of the mailbox health inputs `app/sending/health.py`
weights, and it is also the only real measure of whether the copy works.
It is computed here from rows rather than from a running counter so it
stays correct across worker restarts and matches what the analytics
report shows.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.company import Company
from app.db.models.enums import ReplyClassification, SendStatus
from app.db.models.message import Message
from app.db.models.reply import Reply
from app.db.models.send import Send


class ReplyCreate(BaseModel):
    model_config = ConfigDict(frozen=True)

    send_id: uuid.UUID
    raw_snippet: str
    classification: ReplyClassification | None = None
    received_at: datetime | None = None


class ReplyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, reply: ReplyCreate) -> Reply:
        payload = reply.model_dump()
        payload["received_at"] = reply.received_at or datetime.now(UTC)
        row = Reply(**payload)
        self._session.add(row)
        await self._session.flush()
        return row

    async def get(self, reply_id: uuid.UUID) -> Reply | None:
        return await self._session.get(Reply, reply_id)

    async def classify(self, reply_id: uuid.UUID, classification: ReplyClassification) -> Reply:
        row = await self._session.get(Reply, reply_id)
        if row is None:
            raise ValueError(f"Reply {reply_id} does not exist")
        row.classification = classification
        await self._session.flush()
        return row

    async def unclassified(self, *, limit: int = 200) -> list[Reply]:
        """Replies the classifier has not run against yet."""
        stmt = (
            select(Reply)
            .where(Reply.classification.is_(None))
            .order_by(Reply.received_at, Reply.id)
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def for_campaign(
        self, campaign_id: uuid.UUID, *, limit: int = 100, offset: int = 0
    ) -> list[Reply]:
        stmt = (
            select(Reply)
            .join(Send, Reply.send_id == Send.id)
            .join(Message, Send.message_id == Message.id)
            .join(Company, Message.company_id == Company.id)
            .where(Company.campaign_id == campaign_id)
            .order_by(Reply.received_at.desc(), Reply.id.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def classification_breakdown(self, campaign_id: uuid.UUID) -> dict[str, int]:
        stmt = (
            select(Reply.classification, func.count())
            .join(Send, Reply.send_id == Send.id)
            .join(Message, Send.message_id == Message.id)
            .join(Company, Message.company_id == Company.id)
            .where(Company.campaign_id == campaign_id)
            .group_by(Reply.classification)
        )
        result = await self._session.execute(stmt)
        return {
            (str(kind.value) if kind is not None else "unclassified"): count
            for kind, count in result.all()
        }

    async def reply_rate(self, mailbox_id: uuid.UUID) -> float:
        """Replies per delivered send for one mailbox.

        Returns 0.0 when the mailbox has sent nothing -- an unused mailbox
        has no reply rate, and dividing by zero to report one would be
        inventing a number. Callers treat 0-of-0 as 'no signal yet', not
        as 'performing badly'.
        """
        sent_stmt = (
            select(func.count())
            .select_from(Send)
            .where(Send.mailbox_id == mailbox_id, Send.status == SendStatus.SENT)
        )
        sent = int((await self._session.execute(sent_stmt)).scalar_one())
        if sent == 0:
            return 0.0

        replied_stmt = (
            select(func.count())
            .select_from(Reply)
            .join(Send, Reply.send_id == Send.id)
            .where(Send.mailbox_id == mailbox_id)
        )
        replied = int((await self._session.execute(replied_stmt)).scalar_one())
        return replied / sent


__all__ = ["ReplyCreate", "ReplyRepository"]
