"""ResolverRunRepository — telemetry on every resolver attempt.

This closes the loop that the whole project exists to prevent. The
prototype recorded 1,640 consecutive verification failures, wrote a
results file and exited 0 (CLAUDE.md §2.1). `app/analytics/yield_report.py`
already knows how to read this table and spot a resolver whose hit rate
collapsed -- it has simply never had rows to read, because nothing wrote
any.

Misses and errors are recorded, not just hits. A resolver that returns
nothing 1,000 times in a row is exactly the signal worth surfacing, and it
is invisible if only successes are logged.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.enums import ResolverOutcome
from app.db.models.resolver_run import ResolverRun


class ResolverRunCreate(BaseModel):
    model_config = ConfigDict(frozen=True)

    resolver: str
    field: str
    tier: int
    outcome: ResolverOutcome
    latency_ms: int = Field(ge=0)
    subject_id: uuid.UUID | None = None
    candidates_returned: int = 0
    cost: Decimal = Decimal(0)
    error: str | None = None
    ran_at: datetime | None = None


class ResolverRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, run: ResolverRunCreate) -> ResolverRun:
        payload = run.model_dump()
        payload["ran_at"] = run.ran_at or datetime.now(UTC)
        row = ResolverRun(**payload)
        self._session.add(row)
        await self._session.flush()
        return row

    async def record_many(self, runs: list[ResolverRunCreate]) -> int:
        """Bulk path for a batch stage. Returns the count written so a
        caller can assert telemetry was actually persisted rather than
        assuming it.
        """
        if not runs:
            return 0
        now = datetime.now(UTC)
        rows = []
        for run in runs:
            payload = run.model_dump()
            payload["ran_at"] = run.ran_at or now
            rows.append(ResolverRun(**payload))
        self._session.add_all(rows)
        await self._session.flush()
        return len(rows)


__all__ = ["ResolverRunCreate", "ResolverRunRepository"]
