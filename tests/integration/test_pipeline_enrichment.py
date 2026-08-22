"""Integration tests for the enrichment stage.

The two that matter most are about *classification*, both written after
running the stage against real websites exposed the same bug twice:

  - `test_resolver_errors_reach_the_batch_guard`: run_waterfall never
    raises. It converts a resolver exception into an ERROR attempt on
    purpose, so one dead site cannot kill a run. A stage that therefore
    reads only escaped exceptions counts zero errors forever, and a run
    where every single site failed looks exactly like a clean run that
    found nothing. That is the prototype's failure, reproduced.

  - `test_dead_domains_are_misses_not_errors`: the opposite mistake.
    Directory data goes stale (measured: 10 of 12 real Austin dental
    domains were dead), so counting a lapsed domain as an error would fire
    the guard on every run and make the pipeline unusable.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import BatchAbortedError, DomainNotResolvableError, UpstreamError
from app.db.models.enums import ResolverOutcome, RoleClass, SubjectType
from app.db.models.fact import Fact
from app.db.models.resolver_run import ResolverRun
from app.db.repositories.campaign_repository import CampaignCreate, CampaignRepository
from app.db.repositories.company_repository import CompanyCreate, CompanyRepository
from app.db.repositories.email_repository import EmailAddressRepository
from app.db.repositories.person_repository import PersonRepository
from app.llm.cost import InMemoryCostTracker
from app.resolvers.base import Candidate, LeadContext, Tier
from app.resolvers.registry import build_registry
from app.services.enrichment import run_enrichment
from app.services.factories import Runtime


class StubResolver:
    """A resolver whose behaviour a test dictates outright."""

    def __init__(
        self,
        *,
        name: str,
        field: str,
        candidates: list[Candidate] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.name = name
        self.field = field
        self.tier = Tier.FREE
        self.cost_per_call = __import__("decimal").Decimal("0")
        self.jurisdictions = None
        self._candidates = candidates or []
        self._raises = raises

    async def applicable(self, ctx: LeadContext) -> bool:
        return True

    async def resolve(self, ctx: LeadContext) -> list[Candidate]:
        if self._raises is not None:
            raise self._raises
        return list(self._candidates)


def _runtime(*, error_threshold: float = 0.05) -> Runtime:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url="postgresql+asyncpg://u:p@localhost/db",
        redis_url="redis://localhost:6379/0",
        secret_key="test-secret-key-long-enough",  # type: ignore[arg-type]
        error_rate_threshold=error_threshold,
    )
    return Runtime(
        settings=settings,
        http=None,  # type: ignore[arg-type]
        vault=None,  # type: ignore[arg-type]
        cost_tracker=InMemoryCostTracker(),
        llm=None,
    )


async def _campaign_with_companies(
    session: AsyncSession, *, count: int = 1, with_website: bool = True
) -> uuid.UUID:
    campaign = await CampaignRepository(session).create(
        CampaignCreate(name="c", icp={"categories": ["dentist"], "locations": ["Austin, TX"]})
    )
    await CompanyRepository(session).bulk_upsert(
        [
            CompanyCreate(
                campaign_id=campaign.id,
                name=f"Clinic {i}",
                country_code="US",
                website=f"https://clinic{i}.example" if with_website else None,
                source_place_id=f"p{i}",
            )
            for i in range(count)
        ]
    )
    return campaign.id


def _person(name: str = "Dr Jane Smith") -> Candidate:
    return Candidate(
        value=name,
        confidence=0.9,
        source="website_person",
        source_url="https://clinic0.example/about",
        extra={"title": "Owner", "role_class": RoleClass.OWNER.value},
    )


def _email(address: str = "jane@clinic0.example") -> Candidate:
    return Candidate(
        value=address,
        confidence=0.9,
        source="website_email",
        source_url="https://clinic0.example/contact",
        extra={"strategy": "mailto", "is_role_account": False},
    )


# ---------------------------------------------------------------------------


async def test_enrichment_writes_person_email_and_provenance(
    db_session: AsyncSession,
) -> None:
    """Every value lands in `facts` with its source URL before it is
    promoted to a person or email row (CLAUDE.md 2.2).
    """
    campaign_id = await _campaign_with_companies(db_session)
    registry = build_registry(
        [
            StubResolver(name="website_person", field="person_name", candidates=[_person()]),
            StubResolver(name="website_email", field="email", candidates=[_email()]),
        ]
    )

    result = await run_enrichment(db_session, _runtime(), campaign_id, registry=registry)

    assert result.attempted == 1
    assert result.found == 1
    assert result.written == 1

    company = (await CompanyRepository(db_session).list_for_campaign(campaign_id))[0]

    person = await PersonRepository(db_session).best_for_company(company.id)
    assert person is not None
    assert person.full_name == "Dr Jane Smith"
    assert person.title == "Owner"
    assert person.role_class == RoleClass.OWNER, "role_class survived the round trip"

    emails = await EmailAddressRepository(db_session).for_company(company.id)
    assert [e.address for e in emails] == ["jane@clinic0.example"]
    assert emails[0].person_id == person.id, "the address is attributed to the person"

    facts = (await db_session.execute(select(Fact))).scalars().all()
    assert {f.field for f in facts} == {"person_name", "email"}
    for fact in facts:
        assert fact.source_url is not None
        assert fact.source_url.startswith("https://")
        assert fact.subject_type == SubjectType.COMPANY
        assert 0.0 <= fact.confidence <= 1.0


async def test_resolver_errors_reach_the_batch_guard(db_session: AsyncSession) -> None:
    """The load-bearing regression.

    Before this, a run where every website failed reported errors=0 and a
    clean 0% hit rate — indistinguishable from crawling healthy sites that
    simply had no contact page. The guard could never fire because errors
    never reached it.
    """
    campaign_id = await _campaign_with_companies(db_session, count=4)
    registry = build_registry(
        [
            StubResolver(
                name="website_person",
                field="person_name",
                raises=UpstreamError("503 from origin"),
            ),
            StubResolver(
                name="website_email", field="email", raises=UpstreamError("503 from origin")
            ),
        ]
    )

    with pytest.raises(BatchAbortedError) as caught:
        await run_enrichment(db_session, _runtime(), campaign_id, registry=registry)

    assert "100.0%" in str(caught.value)
    assert "503 from origin" in str(caught.value), "the guard names the actual cause"


async def test_dead_domains_are_misses_not_errors(db_session: AsyncSession) -> None:
    """The opposite mistake, and just as damaging.

    A lapsed domain is a fact about the lead, not a failure of ours.
    Measured on real OSM data, most listed websites were dead; treating
    those as errors fires the guard on every run and makes the pipeline
    unusable.
    """
    campaign_id = await _campaign_with_companies(db_session, count=4)
    registry = build_registry(
        [
            StubResolver(
                name="website_person",
                field="person_name",
                raises=DomainNotResolvableError("host does not resolve"),
            ),
            StubResolver(
                name="website_email",
                field="email",
                raises=DomainNotResolvableError("host does not resolve"),
            ),
        ]
    )

    # Does not raise, even though every single company failed to fetch.
    result = await run_enrichment(db_session, _runtime(), campaign_id, registry=registry)

    assert result.attempted == 4
    assert result.found == 0
    assert len(result.errors) == 0, "a dead domain is not a system error"


async def test_a_company_that_crawls_cleanly_but_has_no_contact_is_a_miss(
    db_session: AsyncSession,
) -> None:
    """Distinct from both of the above: fetched fine, nothing to find.
    This is the honest 0% that the guard must not fire on.
    """
    campaign_id = await _campaign_with_companies(db_session, count=3)
    registry = build_registry(
        [
            StubResolver(name="website_person", field="person_name", candidates=[]),
            StubResolver(name="website_email", field="email", candidates=[]),
        ]
    )

    result = await run_enrichment(db_session, _runtime(), campaign_id, registry=registry)

    assert result.attempted == 3
    assert result.skipped == 3
    assert len(result.errors) == 0
    assert result.hit_rate == 0.0


async def test_partial_success_is_not_an_error(db_session: AsyncSession) -> None:
    """A company where the person resolver failed but the email resolver
    worked has still produced a usable lead.
    """
    campaign_id = await _campaign_with_companies(db_session, count=1)
    registry = build_registry(
        [
            StubResolver(name="website_person", field="person_name", raises=UpstreamError("boom")),
            StubResolver(name="website_email", field="email", candidates=[_email()]),
        ]
    )

    result = await run_enrichment(db_session, _runtime(), campaign_id, registry=registry)

    assert result.found == 1
    assert len(result.errors) == 0


async def test_companies_without_a_website_are_never_attempted(
    db_session: AsyncSession,
) -> None:
    """Not an enrichment failure — there is nothing to fetch. Counting
    them would put a permanent floor under the miss rate and make the hit
    rate meaningless.
    """
    campaign_id = await _campaign_with_companies(db_session, count=3, with_website=False)
    registry = build_registry(
        [StubResolver(name="website_person", field="person_name", candidates=[_person()])]
    )

    result = await run_enrichment(db_session, _runtime(), campaign_id, registry=registry)

    assert result.attempted == 0
    assert result.found == 0


async def test_every_attempt_is_recorded_as_telemetry(db_session: AsyncSession) -> None:
    """Hits and misses both, so the yield report can tell a resolver that
    stopped working from one that was never run.
    """
    campaign_id = await _campaign_with_companies(db_session, count=2)
    registry = build_registry(
        [
            StubResolver(name="website_person", field="person_name", candidates=[_person()]),
            StubResolver(name="website_email", field="email", candidates=[]),
        ]
    )

    await run_enrichment(db_session, _runtime(), campaign_id, registry=registry)

    runs = (await db_session.execute(select(ResolverRun))).scalars().all()
    by_resolver = {r.resolver: r.outcome for r in runs}

    assert by_resolver["website_person"] == ResolverOutcome.HIT
    assert by_resolver["website_email"] == ResolverOutcome.MISS
    assert all(r.subject_id is not None for r in runs), "telemetry is per company"


async def test_telemetry_survives_a_batch_abort(db_session: AsyncSession) -> None:
    """A guard that fires must leave behind the evidence of why, not
    discard it along with the partial results.
    """
    campaign_id = await _campaign_with_companies(db_session, count=3)
    registry = build_registry(
        [StubResolver(name="website_person", field="person_name", raises=UpstreamError("boom"))]
    )

    with pytest.raises(BatchAbortedError):
        await run_enrichment(db_session, _runtime(), campaign_id, registry=registry)

    runs = (await db_session.execute(select(ResolverRun))).scalars().all()
    assert len(runs) == 3
    assert all(r.outcome == ResolverOutcome.ERROR for r in runs)
    assert all(r.error is not None for r in runs)


async def test_nothing_to_enrich_is_not_a_failure(db_session: AsyncSession) -> None:
    campaign_id = await _campaign_with_companies(db_session, count=0)

    result = await run_enrichment(db_session, _runtime(), campaign_id, registry=build_registry([]))

    assert result.attempted == 0
    assert result.error_rate == 0.0


async def test_missing_campaign_raises(db_session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        await run_enrichment(db_session, _runtime(), uuid.uuid4(), registry=build_registry([]))
