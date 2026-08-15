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

    async def get(self, domain: str) -> DomainPattern | None:
        """The learned pattern for a domain, or None if none is known.

        Deliberately not filtered by campaign — that is the whole point of
        this table (see the model's docstring).
        """
        result: DomainPattern | None = await self._session.scalar(
            select(DomainPattern).where(DomainPattern.domain == domain)
        )
        return result

    async def learn(self, domain: str, pattern: str, *, weight: int = 1) -> DomainPattern:
        """Upsert the confirmed pattern for a domain.

        - No row yet: insert with confirmed_count = weight.
        - Row exists with the same pattern: add weight to confirmed_count.
        - Row exists with a *different* pattern (conflict): keep whichever
          carries more evidence. Resolved deterministically by comparing
          counts, never by last-write-wins (CLAUDE.md rule 2.1: no silent
          overwrite of a fact we have evidence for).

        `weight` grades confirmation strength (see
        app/resolvers/email/learning.py:SOURCE_WEIGHTS). A reply actually
        received from an address is proof and carries weight 3, so it can
        correct a pattern that a weaker source got wrong — which is the
        only way a poisoned domain ever recovers on its own.
        """
        existing = await self.get(domain)
        now = datetime.now(UTC)

        if existing is None:
            row = DomainPattern(
                domain=domain, pattern=pattern, confirmed_count=weight, last_confirmed_at=now
            )
            self._session.add(row)
            await self._session.flush()
            return row

        if existing.pattern == pattern:
            existing.confirmed_count += weight
            existing.last_confirmed_at = now
            await self._session.flush()
            return existing

        # Conflict: the new observation only displaces an established
        # pattern when it carries strictly more evidence. Ties keep the
        # incumbent, so a single weak observation can never flip a domain.
        if weight > existing.confirmed_count:
            existing.pattern = pattern
            existing.confirmed_count = weight
            existing.last_confirmed_at = now

        await self._session.flush()
        return existing
