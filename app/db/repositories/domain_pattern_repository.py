"""DomainPatternRepository — the global, cross-campaign pattern table.

Session 09 turns ~9 blind pattern guesses into 1 targeted check once a
domain's format is known. This repository is where a confirmed address
actually gets learned.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.domain_pattern import DomainPattern


class DomainPatternRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def learn(self, domain: str, pattern: str) -> DomainPattern:
        """Upsert the confirmed pattern for a domain.

        - No row yet: insert with confirmed_count=1.
        - Row exists with the same pattern: increment confirmed_count.
        - Row exists with a *different* pattern (conflict): keep whichever
          has the higher confirmed_count. A single new observation carries
          an implicit count of 1, so it only displaces an established
          pattern once it has itself been confirmed at least as many times
          — resolved deterministically, never by last-write-wins (CLAUDE.md
          rule 2.1: no silent overwrite of a fact we have evidence for).
        """
        existing = await self._session.scalar(
            select(DomainPattern).where(DomainPattern.domain == domain)
        )
        now = datetime.now(UTC)

        if existing is None:
            row = DomainPattern(
                domain=domain, pattern=pattern, confirmed_count=1, last_confirmed_at=now
            )
            self._session.add(row)
            await self._session.flush()
            return row

        if existing.pattern == pattern:
            existing.confirmed_count += 1
            existing.last_confirmed_at = now
            await self._session.flush()
            return existing

        # Conflict. existing.confirmed_count is always >= 1, so it never
        # loses to a single new observation — see docstring.
        await self._session.flush()
        return existing
