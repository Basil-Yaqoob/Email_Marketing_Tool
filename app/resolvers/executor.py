"""The waterfall executor.

Tries FREE, then OWNED, then METERED (only if explicitly allowed), stopping
as soon as merged confidence clears the threshold. Every resolver attempt —
hit, miss, error or skip — becomes a ResolverAttempt, so a caller can
persist it to resolver_runs (app/db/models/resolver_run.py) and the yield
dashboard (Session 19) has real telemetry to show.

This module has no dependency on app.db beyond the ResolverOutcome enum
(pure Python, no engine) — it stays testable with fake resolvers and zero
I/O, which is exactly how tests/unit/test_waterfall.py exercises it.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal

from app.core.logging import get_logger
from app.db.models.enums import ResolverOutcome
from app.resolvers.base import Candidate, LeadContext, Resolver, Tier
from app.resolvers.merge import merge_candidates
from app.resolvers.registry import ResolverRegistry

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ResolverAttempt:
    """Telemetry for one resolver's attempt at one lead — the in-memory
    shape of a resolver_runs row, decoupled from SQLAlchemy so this module
    doesn't need a database to be tested.
    """

    resolver: str
    field: str
    tier: Tier
    outcome: ResolverOutcome
    candidates_returned: int
    latency_ms: int
    cost: Decimal
    error: str | None = None


@dataclass(frozen=True, slots=True)
class Resolution:
    best: Candidate | None
    all: list[Candidate]
    stopped_at: Tier | None
    attempts: list[ResolverAttempt]


async def _run_one(resolver: Resolver, ctx: LeadContext) -> tuple[ResolverAttempt, list[Candidate]]:
    start = time.monotonic()

    if not await resolver.applicable(ctx):
        latency_ms = int((time.monotonic() - start) * 1000)
        attempt = ResolverAttempt(
            resolver=resolver.name,
            field=resolver.field,
            tier=resolver.tier,
            outcome=ResolverOutcome.SKIPPED,
            candidates_returned=0,
            latency_ms=latency_ms,
            cost=Decimal("0"),
        )
        return attempt, []

    try:
        candidates = await resolver.resolve(ctx)
    except Exception as exc:  # noqa: BLE001 — deliberately broad: converted to
        # an ERROR telemetry row, never swallowed. One bad source must not
        # kill the lead (CLAUDE.md 2.1), but the failure is still recorded,
        # not hidden — the batch-level abort in app/resolvers/batch.py is
        # what actually enforces "fail loud" across a run.
        latency_ms = int((time.monotonic() - start) * 1000)
        log.warning("resolver.error", resolver=resolver.name, field=resolver.field, error=str(exc))
        attempt = ResolverAttempt(
            resolver=resolver.name,
            field=resolver.field,
            tier=resolver.tier,
            outcome=ResolverOutcome.ERROR,
            candidates_returned=0,
            latency_ms=latency_ms,
            cost=resolver.cost_per_call,
            error=str(exc),
        )
        return attempt, []

    latency_ms = int((time.monotonic() - start) * 1000)
    outcome = ResolverOutcome.HIT if candidates else ResolverOutcome.MISS
    attempt = ResolverAttempt(
        resolver=resolver.name,
        field=resolver.field,
        tier=resolver.tier,
        outcome=outcome,
        candidates_returned=len(candidates),
        latency_ms=latency_ms,
        cost=resolver.cost_per_call,
    )
    return attempt, list(candidates)


async def _gather_with_telemetry(
    pool: list[Resolver], ctx: LeadContext
) -> tuple[list[Candidate], list[ResolverAttempt]]:
    """Run every resolver in the pool concurrently. asyncio.gather with
    return_exceptions=True returns exception *objects* in the results list
    on failure — _run_one() already catches resolver.resolve() exceptions
    internally, but this guards the gather boundary itself too, so a bug
    inside _run_one still produces telemetry instead of vanishing.
    """
    raw_results = await asyncio.gather(*(_run_one(r, ctx) for r in pool), return_exceptions=True)

    candidates: list[Candidate] = []
    attempts: list[ResolverAttempt] = []
    for resolver, result in zip(pool, raw_results, strict=True):
        if isinstance(result, BaseException):
            attempts.append(
                ResolverAttempt(
                    resolver=resolver.name,
                    field=resolver.field,
                    tier=resolver.tier,
                    outcome=ResolverOutcome.ERROR,
                    candidates_returned=0,
                    latency_ms=0,
                    cost=Decimal("0"),
                    error=str(result),
                )
            )
            continue
        attempt, cands = result
        attempts.append(attempt)
        candidates.extend(cands)

    return candidates, attempts


async def run_waterfall(
    *,
    field: str,
    ctx: LeadContext,
    registry: ResolverRegistry,
    threshold: float = 0.85,
    allow_metered: bool = False,
) -> Resolution:
    seen: list[Candidate] = []
    all_attempts: list[ResolverAttempt] = []

    for tier in (Tier.FREE, Tier.OWNED, Tier.METERED):
        if tier is Tier.METERED and not allow_metered:
            break

        pool = registry.select(field=field, tier=tier, ctx=ctx)
        if not pool:
            continue

        new_candidates, attempts = await _gather_with_telemetry(pool, ctx)
        seen += new_candidates
        all_attempts += attempts

        merged = merge_candidates(seen)
        if merged and merged[0].confidence >= threshold:
            return Resolution(best=merged[0], all=merged, stopped_at=tier, attempts=all_attempts)

    return Resolution(best=None, all=merge_candidates(seen), stopped_at=None, attempts=all_attempts)
