"""Unit tests for the waterfall: resolver protocol, registry, merge and the
executor/batch guards. Fake resolvers only — no I/O, no database. See
doc/plans/session-03-waterfall.md; tests 2, 11 and 15 are load-bearing.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from uuid import uuid4

import pytest

from app.core.errors import BatchAbortedError
from app.db.models.enums import ResolverOutcome
from app.resolvers.base import BaseResolver, Candidate, LeadContext, Tier
from app.resolvers.batch import run_batch
from app.resolvers.executor import run_waterfall
from app.resolvers.merge import merge_candidates
from app.resolvers.registry import build_registry

# No module-level `pytestmark = pytest.mark.asyncio`: pyproject.toml sets
# asyncio_mode = "auto", which already marks async def tests automatically.
# This file mixes async (executor/batch) and sync (merge/Candidate) tests,
# and a blanket mark on a sync test is itself a pytest warning.


# --------------------------------------------------------------------------
# Fake resolvers
# --------------------------------------------------------------------------


def _ctx(**overrides: object) -> LeadContext:
    defaults: dict[str, object] = {
        "company_id": uuid4(),
        "company_name": "Acme Dental",
        "domain": "acmedental.com",
        "website": "https://acmedental.com",
        "country_code": "US",
    }
    defaults.update(overrides)
    return LeadContext(**defaults)  # type: ignore[arg-type]


class AlwaysHits(BaseResolver):
    def __init__(
        self,
        name: str,
        *,
        field: str = "person_name",
        tier: Tier = Tier.FREE,
        confidence: float = 0.6,
        value: str = "Jane Smith",
        cost: Decimal = Decimal("0"),
        independence_key: str | None = None,
    ) -> None:
        self.name = name
        self.field = field
        self.tier = tier
        self.cost_per_call = cost
        self.jurisdictions: frozenset[str] | None = None
        self._confidence = confidence
        self._value = value
        self._independence_key = independence_key
        self.call_count = 0

    async def resolve(self, ctx: LeadContext) -> list[Candidate]:
        self.call_count += 1
        return [
            Candidate(
                value=self._value,
                confidence=self._confidence,
                source=self.name,
                independence_key=self._independence_key,
            )
        ]


class AlwaysMisses(BaseResolver):
    def __init__(self, name: str, *, field: str = "person_name", tier: Tier = Tier.FREE) -> None:
        self.name = name
        self.field = field
        self.tier = tier
        self.cost_per_call = Decimal("0")
        self.jurisdictions: frozenset[str] | None = None
        self.call_count = 0

    async def resolve(self, ctx: LeadContext) -> list[Candidate]:
        self.call_count += 1
        return []


class AlwaysErrors(BaseResolver):
    def __init__(
        self,
        name: str,
        *,
        field: str = "person_name",
        tier: Tier = Tier.FREE,
        message: str = "boom",
    ) -> None:
        self.name = name
        self.field = field
        self.tier = tier
        self.cost_per_call = Decimal("0")
        self.jurisdictions: frozenset[str] | None = None
        self.call_count = 0
        self._message = message

    async def resolve(self, ctx: LeadContext) -> list[Candidate]:
        self.call_count += 1
        raise RuntimeError(self._message)


class Slow(BaseResolver):
    def __init__(
        self,
        name: str,
        *,
        delay: float,
        field: str = "person_name",
        tier: Tier = Tier.FREE,
        confidence: float = 0.5,
    ) -> None:
        self.name = name
        self.field = field
        self.tier = tier
        self.cost_per_call = Decimal("0")
        self.jurisdictions: frozenset[str] | None = None
        self._delay = delay
        self._confidence = confidence

    async def resolve(self, ctx: LeadContext) -> list[Candidate]:
        await asyncio.sleep(self._delay)
        return [Candidate(value="Jane Smith", confidence=self._confidence, source=self.name)]


class Metered(BaseResolver):
    def __init__(
        self,
        name: str,
        *,
        field: str = "person_name",
        confidence: float = 0.95,
        cost: Decimal = Decimal("0.05"),
    ) -> None:
        self.name = name
        self.field = field
        self.tier = Tier.METERED
        self.cost_per_call = cost
        self.jurisdictions: frozenset[str] | None = None
        self._confidence = confidence
        self.call_count = 0

    async def resolve(self, ctx: LeadContext) -> list[Candidate]:
        self.call_count += 1
        return [Candidate(value="Jane Smith", confidence=self._confidence, source=self.name)]


class UKOnly(BaseResolver):
    def __init__(self, name: str = "uk_registry", *, field: str = "person_name") -> None:
        self.name = name
        self.field = field
        self.tier = Tier.FREE
        self.cost_per_call = Decimal("0")
        self.jurisdictions: frozenset[str] | None = frozenset({"GB"})
        self.call_count = 0

    async def resolve(self, ctx: LeadContext) -> list[Candidate]:
        self.call_count += 1
        return [Candidate(value="Jane Smith", confidence=0.9, source=self.name)]


class NeverApplicable(BaseResolver):
    """Passes the registry's static jurisdiction gate but declines
    dynamically — e.g. a resolver that only fires once a domain is known.
    """

    def __init__(self, name: str = "needs_domain", *, field: str = "person_name") -> None:
        self.name = name
        self.field = field
        self.tier = Tier.FREE
        self.cost_per_call = Decimal("0")
        self.jurisdictions: frozenset[str] | None = None
        self.resolve_call_count = 0

    async def applicable(self, ctx: LeadContext) -> bool:
        return False

    async def resolve(self, ctx: LeadContext) -> list[Candidate]:
        self.resolve_call_count += 1
        return [Candidate(value="Jane Smith", confidence=0.9, source=self.name)]


class RaisesInApplicable(BaseResolver):
    """A resolver whose applicable() check itself raises — a bug in the
    resolver, not in resolve(). Exercises the asyncio.gather boundary
    guard in _gather_with_telemetry, not _run_one's own try/except.
    """

    def __init__(self, name: str = "broken_applicable", *, field: str = "person_name") -> None:
        self.name = name
        self.field = field
        self.tier = Tier.FREE
        self.cost_per_call = Decimal("0")
        self.jurisdictions: frozenset[str] | None = None

    async def applicable(self, ctx: LeadContext) -> bool:
        raise RuntimeError("applicable() blew up")

    async def resolve(self, ctx: LeadContext) -> list[Candidate]:
        return [Candidate(value="Jane Smith", confidence=0.9, source=self.name)]


# --------------------------------------------------------------------------
# Executor
# --------------------------------------------------------------------------


async def test_free_tier_runs_concurrently_not_serially() -> None:
    resolvers = [Slow(f"slow_{i}", delay=0.1) for i in range(3)]
    registry = build_registry(resolvers)

    start = time.monotonic()
    await run_waterfall(field="person_name", ctx=_ctx(), registry=registry, threshold=0.99)
    elapsed = time.monotonic() - start

    assert elapsed < 0.25, f"three 100ms resolvers took {elapsed:.3f}s — ran serially?"


async def test_stops_at_threshold_without_touching_metered() -> None:
    """The core cost guarantee: a metered resolver's resolve() must never
    be awaited when a cheaper tier already cleared the threshold.
    """
    free = AlwaysHits("free1", confidence=0.9)
    metered = Metered("metered1")
    registry = build_registry([free, metered])

    resolution = await run_waterfall(
        field="person_name", ctx=_ctx(), registry=registry, threshold=0.85, allow_metered=True
    )

    assert resolution.stopped_at == Tier.FREE
    assert metered.call_count == 0


async def test_escalates_to_owned_when_free_below_threshold() -> None:
    free = AlwaysHits("free1", confidence=0.5)
    owned = AlwaysHits("owned1", tier=Tier.OWNED, confidence=0.9)
    registry = build_registry([free, owned])

    resolution = await run_waterfall(
        field="person_name", ctx=_ctx(), registry=registry, threshold=0.85
    )

    assert resolution.stopped_at == Tier.OWNED
    assert free.call_count == 1
    assert owned.call_count == 1


async def test_metered_skipped_when_allow_metered_false() -> None:
    free = AlwaysHits("free1", confidence=0.5)
    metered = Metered("metered1", confidence=0.95)
    registry = build_registry([free, metered])

    resolution = await run_waterfall(
        field="person_name", ctx=_ctx(), registry=registry, threshold=0.85, allow_metered=False
    )

    assert metered.call_count == 0
    assert resolution.best is None


async def test_one_failing_resolver_does_not_kill_the_lead() -> None:
    good = AlwaysHits("good", confidence=0.9)
    bad = AlwaysErrors("bad")
    registry = build_registry([good, bad])

    resolution = await run_waterfall(
        field="person_name", ctx=_ctx(), registry=registry, threshold=0.85
    )

    assert resolution.best is not None
    assert resolution.best.confidence == pytest.approx(0.9)
    error_attempts = [a for a in resolution.attempts if a.outcome == ResolverOutcome.ERROR]
    assert len(error_attempts) == 1


async def test_telemetry_row_written_for_a_miss() -> None:
    """Misses recorded, not just hits — the anti-silent-failure guarantee
    at resolver granularity.
    """
    miss = AlwaysMisses("miss1")
    registry = build_registry([miss])

    resolution = await run_waterfall(
        field="person_name", ctx=_ctx(), registry=registry, threshold=0.85
    )

    assert len(resolution.attempts) == 1
    assert resolution.attempts[0].outcome == ResolverOutcome.MISS
    assert resolution.attempts[0].candidates_returned == 0


async def test_telemetry_records_latency_and_cost() -> None:
    free = AlwaysHits("free1", confidence=0.5)  # below threshold so metered runs
    metered = Metered("metered1", cost=Decimal("0.05"))
    registry = build_registry([free, metered])

    resolution = await run_waterfall(
        field="person_name", ctx=_ctx(), registry=registry, threshold=0.85, allow_metered=True
    )

    metered_attempt = next(a for a in resolution.attempts if a.resolver == "metered1")
    assert metered_attempt.cost == Decimal("0.05")
    assert metered_attempt.latency_ms >= 0


async def test_dynamically_inapplicable_resolver_is_skipped_not_called() -> None:
    """A resolver that clears the registry's static jurisdiction gate but
    declines dynamically (applicable() -> False) must be recorded as
    SKIPPED and never have resolve() awaited.
    """
    skip = NeverApplicable()
    registry = build_registry([skip])

    resolution = await run_waterfall(
        field="person_name", ctx=_ctx(), registry=registry, threshold=0.85
    )

    assert skip.resolve_call_count == 0
    assert len(resolution.attempts) == 1
    assert resolution.attempts[0].outcome == ResolverOutcome.SKIPPED


async def test_exception_in_applicable_check_recorded_as_error() -> None:
    """A bug in applicable() itself (not resolve()) is caught at the
    asyncio.gather boundary in _gather_with_telemetry, not inside
    _run_one's own try/except — must still produce an ERROR attempt rather
    than propagating out of the waterfall.
    """
    broken = RaisesInApplicable()
    registry = build_registry([broken])

    resolution = await run_waterfall(
        field="person_name", ctx=_ctx(), registry=registry, threshold=0.85
    )

    assert len(resolution.attempts) == 1
    assert resolution.attempts[0].outcome == ResolverOutcome.ERROR
    assert "blew up" in (resolution.attempts[0].error or "")


# --------------------------------------------------------------------------
# Registry / jurisdiction
# --------------------------------------------------------------------------


async def test_uk_only_resolver_skipped_for_us_lead() -> None:
    uk = UKOnly()
    registry = build_registry([uk])

    pool = registry.select(field="person_name", tier=Tier.FREE, ctx=_ctx(country_code="US"))

    assert pool == []
    assert uk.call_count == 0


# --------------------------------------------------------------------------
# Merge
# --------------------------------------------------------------------------


def test_two_independent_sources_combine_confidence() -> None:
    c1 = Candidate(value="Jane Smith", confidence=0.6, source="a")
    c2 = Candidate(value="Jane Smith", confidence=0.6, source="b")

    merged = merge_candidates([c1, c2])

    assert len(merged) == 1
    assert merged[0].confidence == pytest.approx(0.84)


def test_shared_independence_key_does_not_stack() -> None:
    c1 = Candidate(
        value="Jane Smith", confidence=0.6, source="serp1", independence_key="serp_index"
    )
    c2 = Candidate(
        value="Jane Smith", confidence=0.6, source="serp2", independence_key="serp_index"
    )

    merged = merge_candidates([c1, c2])

    assert len(merged) == 1
    assert merged[0].confidence == pytest.approx(0.6)


def test_confidence_capped_at_099() -> None:
    candidates = [Candidate(value="Jane Smith", confidence=0.9, source=f"s{i}") for i in range(5)]

    merged = merge_candidates(candidates)

    assert merged[0].confidence == pytest.approx(0.99)
    assert merged[0].confidence < 1.0


def test_values_normalised_before_grouping() -> None:
    c1 = Candidate(value="Dr. Jane Smith", confidence=0.6, source="a")
    c2 = Candidate(value="jane smith", confidence=0.6, source="b")

    def normalise(v: str) -> str:
        return v.lower().removeprefix("dr. ")

    merged = merge_candidates([c1, c2], normalise=normalise)

    assert len(merged) == 1
    assert merged[0].confidence == pytest.approx(0.84)


def test_candidate_rejects_confidence_above_one() -> None:
    with pytest.raises(ValueError, match="outside"):
        Candidate(value="x", confidence=1.5, source="test")


# --------------------------------------------------------------------------
# Batch — the anti-silent-failure guard
# --------------------------------------------------------------------------


async def test_batch_aborts_past_error_threshold() -> None:
    bad = AlwaysErrors("bad")
    registry = build_registry([bad])
    contexts = [_ctx() for _ in range(10)]

    with pytest.raises(BatchAbortedError) as exc_info:
        await run_batch(
            field="person_name",
            contexts=contexts,
            registry=registry,
            min_checked=5,
            error_threshold=0.05,
        )

    assert exc_info.value.completed < 10
    assert exc_info.value.error_rate == 1.0


async def test_batch_does_not_abort_before_min_checked() -> None:
    bad = AlwaysErrors("bad")
    registry = build_registry([bad])
    contexts = [_ctx() for _ in range(2)]

    result = await run_batch(
        field="person_name", contexts=contexts, registry=registry, min_checked=20
    )

    assert result.checked == 2
    assert result.errors == 2


async def test_batch_completes_normally_with_hits() -> None:
    """The happy path: a batch that actually resolves leads returns a
    BatchResult without raising, and counts hits correctly.
    """
    good = AlwaysHits("good", confidence=0.9)
    registry = build_registry([good])
    contexts = [_ctx() for _ in range(5)]

    result = await run_batch(field="person_name", contexts=contexts, registry=registry)

    assert result.checked == 5
    assert result.hits == 5
    assert result.errors == 0


async def test_batch_aborts_on_zero_yield_with_no_errors() -> None:
    """The exact shape of the prototype's bug: no errors, no output,
    nothing raised — until this guard.
    """
    miss = AlwaysMisses("miss1")
    registry = build_registry([miss])
    contexts = [_ctx() for _ in range(3)]

    with pytest.raises(BatchAbortedError) as exc_info:
        await run_batch(field="person_name", contexts=contexts, registry=registry, min_checked=20)

    assert exc_info.value.completed == 3
    assert exc_info.value.error_rate == 0.0
