"""HookRepository — the only way a mined hook (or the record of finding
none) reaches the `hooks` table.

Decoupled from HookMiningAgent on purpose, same pattern as every resolver
session since 03: the agent returns a pure HookResult, this repository
persists it. Nothing in this codebase yet calls both in sequence for a
real campaign — see doc/02-ROADMAP.md Discovered work, same open item as
FactRepository and DomainPatternRepository before it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.hooks.types import HookResult
from app.db.models.hook import Hook


class HookRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, company_id: uuid.UUID, result: HookResult) -> Hook:
        row = Hook(
            company_id=company_id,
            status=result.status,
            hook_text=result.hook_text,
            source_url=result.source_url,
            event_date=result.event_date,
            news_type=result.news_type,
            channel=result.channel,
            confidence=result.confidence,
            swap_test_passed=result.swap_test_passed,
            needs_review=result.needs_review,
            notes=result.notes,
            transcript=result.transcript or None,
            model=result.model or None,
            cost_usd=result.cost_usd or None,
            found_at=datetime.now(UTC),
        )
        self._session.add(row)
        await self._session.flush()
        return row
