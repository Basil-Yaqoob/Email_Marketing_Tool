"""MessageRepository — the only way a drafted email (Session 13) reaches
the `messages` table.

Decoupled from `CopywritingAgent` on purpose, same pattern as
`HookRepository` before it: the agent returns a pure `MessageResult`, this
repository persists it. Everything the review queue (Session 21) will need
— the strategist's reasoning, the critic's score and fabrication findings,
the mechanical scan, and the full revision history — travels in
`quality_report` rather than being re-derived from a live model call.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.copy.types import MessageResult
from app.db.models.enums import MessageStatus
from app.db.models.message import Message


class MessageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(
        self,
        company_id: uuid.UUID,
        person_id: uuid.UUID | None,
        result: MessageResult,
    ) -> Message:
        row = Message(
            company_id=company_id,
            person_id=person_id,
            angle=result.angle,
            subject=result.subject,
            body=result.body,
            quality_report={
                "passed": result.passed,
                "score": result.score,
                "strategy": result.strategy,
                "critique": result.critique,
                "scan": result.scan,
                "attempts": result.attempts,
                "model": result.model,
                "cost_usd": result.cost_usd,
            },
            # Review/approval is Session 21's job; every freshly written
            # message lands here as a draft regardless of whether it
            # passed the pipeline's own gate -- `passed` inside
            # quality_report is what the review queue reads to prioritise,
            # not a reason to withhold a row.
            status=MessageStatus.DRAFT,
        )
        self._session.add(row)
        await self._session.flush()
        return row
