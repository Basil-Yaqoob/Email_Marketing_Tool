"""Per-call cost recording, pre-run estimates, and a spend cap that
raises.

A surprise bill is the metered-dependency failure this whole project
exists to avoid, so the cap is an exception (BudgetExceededError), never a
warning. CLAUDE.md rule 2.3.

`estimate()` powers a pre-run preview from *measured* history. When there
is no history for a task it falls back to a coarse prior and says so —
`Estimate.is_measured` is False, and the UI must not present a guess as if
it were data. Showing a made-up number confidently is how a user ends up
surprised anyway.

**Persistence is not implemented here.** CostTracker is a Protocol and the
shipped implementation is in-memory, matching the pattern of every
resolver session since 03: the logic is built and tested, the database
wiring waits for the session that first runs a real campaign. The
consequence is real and worth stating — an in-memory cap resets when the
process restarts, so it bounds a run, not a month. See doc/02-ROADMAP.md
Discovered work.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from app.core.errors import BudgetExceededError
from app.llm.types import Estimate, Task, Usage

# Coarse per-lead priors, used only until real history exists. Deliberately
# pessimistic: an estimate that comes in under budget is a pleasant
# surprise, one that comes in over is the exact failure being guarded.
FALLBACK_COST_PER_CALL: dict[Task, Decimal] = {
    Task.EXTRACT: Decimal("0.0005"),
    Task.CLASSIFY: Decimal("0.0005"),
    Task.RESEARCH: Decimal("0.03"),
    Task.STRATEGY: Decimal("0.01"),
    Task.COPY: Decimal("0.02"),
    Task.CRITIQUE: Decimal("0.01"),
}


@dataclass(frozen=True, slots=True)
class LLMCall:
    task: Task
    model: str
    provider: str
    usage: Usage
    cost_usd: Decimal
    latency_ms: int
    campaign_id: UUID | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class CostTracker(Protocol):
    async def record(self, call: LLMCall) -> None: ...
    async def campaign_total(self, campaign_id: UUID) -> Decimal: ...
    async def estimate(self, task: Task, n_leads: int) -> Estimate: ...


class InMemoryCostTracker:
    """The shipped CostTracker. See this module's docstring on persistence."""

    def __init__(self) -> None:
        self._calls: list[LLMCall] = []
        self._by_campaign: dict[UUID, Decimal] = defaultdict(lambda: Decimal("0"))

    async def record(self, call: LLMCall) -> None:
        self._calls.append(call)
        if call.campaign_id is not None:
            self._by_campaign[call.campaign_id] += call.cost_usd

    async def campaign_total(self, campaign_id: UUID) -> Decimal:
        return self._by_campaign.get(campaign_id, Decimal("0"))

    async def estimate(self, task: Task, n_leads: int) -> Estimate:
        observed = [c.cost_usd for c in self._calls if c.task == task]
        if observed:
            avg = sum(observed, Decimal("0")) / Decimal(len(observed))
            based_on = len(observed)
        else:
            avg = FALLBACK_COST_PER_CALL[task]
            based_on = 0

        return Estimate(
            task=task,
            n_leads=n_leads,
            avg_cost_usd=avg,
            total_usd=avg * Decimal(n_leads),
            based_on_calls=based_on,
        )

    @property
    def calls(self) -> tuple[LLMCall, ...]:
        return tuple(self._calls)


class SpendCap:
    """Refuses to start a call once a campaign is at or over its limit.

    Checked *before* the call, not after: the point is to not spend the
    money, and a post-hoc check has already spent it. That means the cap
    can be overshot by at most one call, which is the honest trade — the
    exact cost of a call is not knowable before making it.
    """

    def __init__(self, *, limit_usd: Decimal, tracker: CostTracker) -> None:
        self._limit = limit_usd
        self._tracker = tracker

    async def check(self, campaign_id: UUID | None) -> None:
        if campaign_id is None:
            return
        spent = await self._tracker.campaign_total(campaign_id)
        if spent >= self._limit:
            raise BudgetExceededError(
                f"campaign {campaign_id} has spent ${spent} of its ${self._limit} "
                "LLM budget; refusing to start another call"
            )
