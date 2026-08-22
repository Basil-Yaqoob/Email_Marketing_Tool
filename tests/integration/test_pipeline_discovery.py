"""Integration tests for the discovery pipeline stage.

This is the first code in the project that goes campaign -> resolver ->
database. The resolvers are faked here (network in tests is always mocked,
doc/03-TESTING.md); what is real is the orchestration, the persistence,
the idempotency, and the telemetry.

The load-bearing test is `test_zero_results_still_records_telemetry`: a
stage that finds nothing must leave a durable trace saying so, because
"found nothing" and "never ran" look identical afterwards otherwise, and
telling them apart is the entire reason this product exists.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import BatchAbortedError
from app.db.models.enums import CampaignStatus, ResolverOutcome
from app.db.models.resolver_run import ResolverRun
from app.db.repositories.campaign_repository import CampaignCreate, CampaignRepository
from app.db.repositories.company_repository import CompanyRepository
from app.llm.cost import InMemoryCostTracker
from app.resolvers.discovery.base import CompanyCandidate, DiscoverySpec
from app.services.factories import Runtime
from app.services.pipeline import StageResult, guard_error_rate, run_discovery


class FakeDiscoveryResolver:
    """Stands in for OSM. Records the spec it was handed so a test can
    assert the ICP was translated correctly.
    """

    name = "osm"

    def __init__(self, candidates: list[CompanyCandidate] | None = None) -> None:
        self._candidates = candidates or []
        self.specs: list[DiscoverySpec] = []
        self.calls = 0

    async def discover(self, spec: DiscoverySpec) -> list[CompanyCandidate]:
        self.calls += 1
        self.specs.append(spec)
        return list(self._candidates)


class ExplodingResolver:
    name = "osm"

    async def discover(self, spec: DiscoverySpec) -> list[CompanyCandidate]:
        raise RuntimeError("overpass returned 504")


def _candidate(name: str, *, place_id: str, website: str | None = None) -> CompanyCandidate:
    return CompanyCandidate(
        name=name,
        country_code="US",
        website=website,
        source_place_id=place_id,
        source="osm",
    )


def _runtime() -> Runtime:
    """A Runtime with no HTTP client or LLM: the fake resolvers are
    injected directly, so nothing here can reach the network.
    """
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url="postgresql+asyncpg://u:p@localhost/db",
        redis_url="redis://localhost:6379/0",
        secret_key="test-secret-key-long-enough",  # type: ignore[arg-type]
    )
    return Runtime(
        settings=settings,
        http=None,  # type: ignore[arg-type]
        vault=None,  # type: ignore[arg-type]
        cost_tracker=InMemoryCostTracker(),
        llm=None,
    )


async def _campaign(session: AsyncSession, *, icp: dict[str, object] | None = None) -> uuid.UUID:
    campaign = await CampaignRepository(session).create(
        CampaignCreate(
            name="Austin Dentists",
            icp=icp
            if icp is not None
            else {"categories": ["dentist"], "locations": ["Austin, TX"]},
            jurisdiction="US",
        )
    )
    return campaign.id


async def _run(
    session: AsyncSession,
    campaign_id: uuid.UUID,
    resolver: object,
    monkeypatch: pytest.MonkeyPatch,
    **kwargs: object,
) -> StageResult:
    monkeypatch.setattr(
        "app.services.pipeline._build_discovery_resolvers",
        lambda runtime, http: (resolver, None),
    )
    return await run_discovery(session, _runtime(), campaign_id, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------


async def test_discovery_writes_companies_to_the_database(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign_id = await _campaign(db_session)
    resolver = FakeDiscoveryResolver(
        [
            _candidate("Riverside Dental", place_id="p1", website="https://a.example"),
            _candidate("Cedar Park Dentistry", place_id="p2"),
        ]
    )

    result = await _run(db_session, campaign_id, resolver, monkeypatch)

    assert result.found == 2
    assert result.written == 2
    assert result.skipped == 0

    companies = await CompanyRepository(db_session).list_for_campaign(campaign_id)
    assert {c.name for c in companies} == {"Riverside Dental", "Cedar Park Dentistry"}

    funnel = await CampaignRepository(db_session).funnel(campaign_id)
    assert funnel.discovered == 2
    assert funnel.with_website == 1


async def test_icp_is_translated_into_the_discovery_spec(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign_id = await _campaign(
        db_session,
        icp={
            "categories": ["dentist", "orthodontist"],
            "locations": ["Austin, TX"],
            "filters": {"min_rating": 4.0, "min_reviews": 10, "max_reviews": 500},
        },
    )
    resolver = FakeDiscoveryResolver()

    await _run(db_session, campaign_id, resolver, monkeypatch, max_results=42)

    spec = resolver.specs[0]
    assert list(spec.categories) == ["dentist", "orthodontist"]
    assert spec.location == "Austin, TX"
    assert spec.max_results == 42
    assert spec.min_rating == 4.0
    assert spec.min_reviews == 10
    assert spec.max_reviews == 500


async def test_rerunning_discovery_does_not_duplicate(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clicking the button twice must not double the lead list."""
    campaign_id = await _campaign(db_session)
    candidates = [_candidate(f"Clinic {i}", place_id=f"p{i}") for i in range(3)]

    first = await _run(db_session, campaign_id, FakeDiscoveryResolver(candidates), monkeypatch)
    second = await _run(db_session, campaign_id, FakeDiscoveryResolver(candidates), monkeypatch)

    assert first.written == 3
    assert second.written == 0
    assert second.skipped == 3
    assert (await CampaignRepository(db_session).funnel(campaign_id)).discovered == 3


async def test_zero_results_still_records_telemetry(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The load-bearing one.

    A stage that finds nothing must leave a durable MISS row. Without it,
    'the resolver ran and found nothing' is indistinguishable afterwards
    from 'the resolver never ran' — which is precisely how the prototype
    recorded 1,640 consecutive failures and still exited 0.
    """
    campaign_id = await _campaign(db_session)

    result = await _run(db_session, campaign_id, FakeDiscoveryResolver([]), monkeypatch)

    assert result.found == 0
    assert result.written == 0

    runs = (await db_session.execute(select(ResolverRun))).scalars().all()
    assert len(runs) == 1
    assert runs[0].outcome == ResolverOutcome.MISS
    assert runs[0].candidates_returned == 0
    assert runs[0].ran_at is not None


async def test_successful_discovery_records_a_hit_per_source(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign_id = await _campaign(db_session)
    resolver = FakeDiscoveryResolver([_candidate("A", place_id="p1")])

    await _run(db_session, campaign_id, resolver, monkeypatch)

    row = (await db_session.execute(select(ResolverRun))).scalars().one()
    assert row.resolver == "osm"
    assert row.outcome == ResolverOutcome.HIT
    assert row.candidates_returned == 1


async def test_resolver_failure_records_an_error_row_and_reraises(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash must be visible in the yield report, not only in a log
    line that scrolled past.
    """
    campaign_id = await _campaign(db_session)

    with pytest.raises(RuntimeError, match="overpass returned 504"):
        await _run(db_session, campaign_id, ExplodingResolver(), monkeypatch)

    row = (await db_session.execute(select(ResolverRun))).scalars().one()
    assert row.outcome == ResolverOutcome.ERROR
    assert row.error is not None
    assert "504" in row.error


async def test_campaign_status_returns_to_draft_after_a_failure(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A campaign stuck in DISCOVERING forever would look like it was
    still working when nothing is running.
    """
    campaign_id = await _campaign(db_session)

    with pytest.raises(RuntimeError):
        await _run(db_session, campaign_id, ExplodingResolver(), monkeypatch)

    campaign = await CampaignRepository(db_session).get(campaign_id)
    assert campaign is not None
    assert campaign.status == CampaignStatus.DRAFT


async def test_icp_without_categories_or_locations_raises(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refusing beats defaulting to something broad: 'everything,
    everywhere' would burn rate limits and fill the database with leads
    nobody asked for.
    """
    no_categories = await _campaign(db_session, icp={"locations": ["Austin, TX"]})
    with pytest.raises(ValueError, match="no categories"):
        await _run(db_session, no_categories, FakeDiscoveryResolver(), monkeypatch)

    no_locations = await _campaign(db_session, icp={"categories": ["dentist"]})
    with pytest.raises(ValueError, match="no locations"):
        await _run(db_session, no_locations, FakeDiscoveryResolver(), monkeypatch)


async def test_missing_campaign_raises(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        await _run(db_session, uuid.uuid4(), FakeDiscoveryResolver(), monkeypatch)


async def test_candidates_without_a_place_id_are_all_kept(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NULL never conflicts in SQL, so sources with no stable id insert
    every time rather than being silently deduped into one row.
    """
    campaign_id = await _campaign(db_session)
    candidates = [
        CompanyCandidate(name=f"Nameless {i}", country_code="US", source="osm") for i in range(3)
    ]

    result = await _run(db_session, campaign_id, FakeDiscoveryResolver(candidates), monkeypatch)

    assert result.written == 3


# ---------------------------------------------------------------------------
# The batch guard
# ---------------------------------------------------------------------------


def test_guard_aborts_past_the_threshold() -> None:
    result = StageResult(stage="enrichment", attempted=100, errors=["boom"] * 8)

    with pytest.raises(BatchAbortedError) as caught:
        guard_error_rate(result, threshold=0.05)

    assert "8.0%" in str(caught.value)
    assert caught.value.completed == 92
    assert caught.value.total == 100


def test_guard_allows_an_error_rate_under_the_threshold() -> None:
    result = StageResult(stage="enrichment", attempted=100, errors=["boom"] * 3)
    guard_error_rate(result, threshold=0.05)  # does not raise


def test_guard_on_an_empty_stage_does_not_divide_by_zero() -> None:
    guard_error_rate(StageResult(stage="enrichment"), threshold=0.05)


def test_stage_result_separates_attempted_from_found() -> None:
    """Attempted-400-found-0 and attempted-0 are different situations, and
    collapsing them into '0 results' is how a silent failure hides.
    """
    nothing_ran = StageResult(stage="s", attempted=0)
    everything_missed = StageResult(stage="s", attempted=400, found=0)

    assert nothing_ran.hit_rate == 0.0
    assert everything_missed.hit_rate == 0.0
    assert nothing_ran.attempted != everything_missed.attempted
