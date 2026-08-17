"""Integration tests against real Postgres — see tests/integration/conftest.py.

Covers the two schema decisions the whole product depends on: facts is
append-only (never UPDATE, always supersede), and domain_patterns is global
across campaigns. Also proves the migration round-trips cleanly and that
the constraints CLAUDE.md calls out (country_code NOT NULL, confidence in
[0,1]) are enforced by the database itself, not just application code.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import asyncpg
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.hooks.types import HookResult
from app.core.config import Settings
from app.db.models.campaign import Campaign
from app.db.models.company import Company
from app.db.models.email import EmailAddress
from app.db.models.enums import (
    CampaignStatus,
    HookChannel,
    HookConfidenceLevel,
    HookNewsType,
    HookVerdict,
    ResolverOutcome,
    SubjectType,
    VerifyStatus,
)
from app.db.models.fact import Fact
from app.db.models.hook import Hook
from app.db.models.resolver_run import ResolverRun
from app.db.repositories.domain_pattern_repository import DomainPatternRepository
from app.db.repositories.fact_repository import FactCreate, FactRepository
from app.db.repositories.hook_repository import HookRepository
from app.resolvers.email.learning import (
    SOURCE_WEIGHTS,
    ConfirmationSource,
    learn_from_confirmed,
)
from app.resolvers.email.patterns import KnownPattern, generate

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# Test 1: migrations apply and rollback cleanly.
#
# This runs against its own throwaway database, deliberately separate from
# the shared `_migrated_schema` fixture the other 11 tests use — it needs
# to downgrade to base, which would rip the schema out from under every
# other test if they shared a database.
# --------------------------------------------------------------------------


async def test_migrations_apply_and_rollback_cleanly() -> None:
    import asyncio

    from alembic import command

    from tests.integration.conftest import _alembic_config, _ensure_database_exists

    settings = Settings()
    base, _, _ = settings.database_url.rpartition("/")
    dbname = f"migration_roundtrip_test_{uuid.uuid4().hex[:8]}"
    url = f"{base}/{dbname}"

    await _ensure_database_exists(url)
    cfg = _alembic_config(url)

    try:
        # See conftest.py's _migrated_schema for why this must run off-thread:
        # alembic's env.py calls asyncio.run() internally, which cannot be
        # invoked from inside this already-running test event loop.
        await asyncio.to_thread(command.upgrade, cfg, "head")

        from sqlalchemy.engine import make_url

        parsed = make_url(url)
        conn = await asyncpg.connect(
            user=parsed.username,
            password=parsed.password,
            host=parsed.host,
            port=parsed.port or 5432,
            database=parsed.database,
        )
        try:
            tables = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            table_names = {row["tablename"] for row in tables}
            assert "campaigns" in table_names
            assert "facts" in table_names
        finally:
            await conn.close()

        await asyncio.to_thread(command.downgrade, cfg, "base")

        conn = await asyncpg.connect(
            user=parsed.username,
            password=parsed.password,
            host=parsed.host,
            port=parsed.port or 5432,
            database=parsed.database,
        )
        try:
            tables = await conn.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                "AND tablename != 'alembic_version'"
            )
            assert len(tables) == 0, f"downgrade base left tables behind: {tables}"

            types = await conn.fetch("SELECT typname FROM pg_type WHERE typtype = 'e'")
            assert len(types) == 0, f"downgrade base left orphaned enum types: {types}"
        finally:
            await conn.close()
    finally:
        # Drop the throwaway database; must connect to the maintenance db
        # to do it, and terminate any lingering backends first.
        maint = await asyncpg.connect(
            user=parsed.username,
            password=parsed.password,
            host=parsed.host,
            port=parsed.port or 5432,
            database="postgres",
        )
        try:
            await maint.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = $1 AND pid <> pg_backend_pid()",
                dbname,
            )
            await maint.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
        finally:
            await maint.close()


# --------------------------------------------------------------------------
# facts: append-only, supersede semantics.
# --------------------------------------------------------------------------


def _fact(**overrides: object) -> FactCreate:
    defaults: dict[str, object] = {
        "subject_type": SubjectType.COMPANY,
        "subject_id": uuid.uuid4(),
        "field": "person_name",
        "value": "Jane Smith",
        "confidence": 0.8,
        "source": "impressum",
        "source_url": "https://example.com/impressum",
        "retrieved_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return FactCreate(**defaults)  # type: ignore[arg-type]


async def test_fact_record_never_updates(db_session: AsyncSession) -> None:
    """Recording the same (subject, field) twice yields two rows, not one
    row updated in place — CLAUDE.md rule 2.2.
    """
    repo = FactRepository(db_session)
    subject_id = uuid.uuid4()

    first = await repo.record(_fact(subject_id=subject_id, value="Jane Smith"))
    second = await repo.record(_fact(subject_id=subject_id, value="Jane A. Smith"))

    assert first.id != second.id
    stmt = select(Fact).where(Fact.subject_id == subject_id)
    rows = (await db_session.execute(stmt)).scalars().all()
    assert len(rows) == 2


async def test_supersede_links_old_to_new_and_is_atomic(db_session: AsyncSession) -> None:
    repo = FactRepository(db_session)
    subject_id = uuid.uuid4()

    old = await repo.record(_fact(subject_id=subject_id, value="Jane Smith", confidence=0.6))
    new = await repo.supersede(
        old.id, _fact(subject_id=subject_id, value="Jane A. Smith", confidence=0.9)
    )

    await db_session.refresh(old)
    assert old.superseded_by == new.id
    assert new.superseded_by is None
    # The old row still exists with its original value — never edited.
    assert old.value == "Jane Smith"


async def test_current_facts_excludes_superseded(db_session: AsyncSession) -> None:
    repo = FactRepository(db_session)
    subject_id = uuid.uuid4()

    old = await repo.record(_fact(subject_id=subject_id, field="email", value="old@x.com"))
    await repo.supersede(old.id, _fact(subject_id=subject_id, field="email", value="new@x.com"))

    current = await repo.current(subject_id, "email")
    values = {f.value for f in current}
    assert values == {"new@x.com"}


async def test_current_facts_ordered_by_confidence_desc(db_session: AsyncSession) -> None:
    repo = FactRepository(db_session)
    subject_id = uuid.uuid4()

    await repo.record(_fact(subject_id=subject_id, field="title", value="low", confidence=0.3))
    await repo.record(_fact(subject_id=subject_id, field="title", value="high", confidence=0.95))
    await repo.record(_fact(subject_id=subject_id, field="title", value="mid", confidence=0.6))

    current = await repo.current(subject_id, "title")
    assert [f.value for f in current] == ["high", "mid", "low"]


async def test_fact_confidence_out_of_range_rejected(db_session: AsyncSession) -> None:
    """The ck_facts_conf CheckConstraint fires at the database level — a
    defense-in-depth guard independent of FactCreate's own Pydantic bounds,
    exercised here by inserting the ORM row directly.
    """
    bad = Fact(
        subject_type=SubjectType.COMPANY,
        subject_id=uuid.uuid4(),
        field="confidence_test",
        value="x",
        confidence=1.5,  # type: ignore[arg-type]
        source="test",
        retrieved_at=datetime.now(UTC),
    )
    db_session.add(bad)
    with pytest.raises(IntegrityError):
        await db_session.flush()


# --------------------------------------------------------------------------
# domain_patterns: global, not scoped to any campaign.
# --------------------------------------------------------------------------


async def test_domain_pattern_is_global_not_per_campaign(db_session: AsyncSession) -> None:
    """Learning a pattern while working lead A of one campaign and reading
    it while working lead B of an unrelated campaign returns the same row
    — domain_patterns has no campaign_id at all.
    """
    repo = DomainPatternRepository(db_session)
    domain = f"{uuid.uuid4().hex[:8]}.example.com"

    await repo.learn(domain, "{first}.{last}")
    # A second, unrelated "campaign" context reads the same domain.
    again = await repo.learn(domain, "{first}.{last}")

    assert again.confirmed_count == 2
    assert not hasattr(again, "campaign_id")


async def test_domain_pattern_learn_increments_on_match(db_session: AsyncSession) -> None:
    repo = DomainPatternRepository(db_session)
    domain = f"{uuid.uuid4().hex[:8]}.example.com"

    first = await repo.learn(domain, "{f}{last}")
    assert first.confirmed_count == 1

    second = await repo.learn(domain, "{f}{last}")
    assert second.confirmed_count == 2
    assert second.id == first.id


async def test_domain_pattern_conflict_keeps_higher_count(db_session: AsyncSession) -> None:
    repo = DomainPatternRepository(db_session)
    domain = f"{uuid.uuid4().hex[:8]}.example.com"

    await repo.learn(domain, "{first}.{last}")
    await repo.learn(domain, "{first}.{last}")
    await repo.learn(domain, "{first}.{last}")  # confirmed_count now 3

    conflicting = await repo.learn(domain, "{f}{last}")  # a single conflicting observation

    assert conflicting.pattern == "{first}.{last}"
    assert conflicting.confirmed_count == 3


async def test_domain_pattern_get_reads_back_what_learn_wrote(db_session: AsyncSession) -> None:
    repo = DomainPatternRepository(db_session)
    domain = f"{uuid.uuid4().hex[:8]}.example.com"

    await repo.learn(domain, "{f}{last}")
    fetched = await repo.get(domain)

    assert fetched is not None
    assert fetched.pattern == "{f}{last}"
    assert fetched.confirmed_count == 1


async def test_weak_observation_cannot_flip_an_established_pattern(
    db_session: AsyncSession,
) -> None:
    """The other half of the guard: a single website mention must never
    displace a pattern a reply already confirmed.
    """
    repo = DomainPatternRepository(db_session)
    domain = f"{uuid.uuid4().hex[:8]}.example.com"

    await repo.learn(domain, "{first}.{last}", weight=SOURCE_WEIGHTS[ConfirmationSource.REPLY])
    unchanged = await repo.learn(
        domain, "{f}{last}", weight=SOURCE_WEIGHTS[ConfirmationSource.WEBSITE]
    )

    assert unchanged.pattern == "{first}.{last}"
    assert unchanged.confirmed_count == 3


async def test_learning_pipeline_end_to_end(db_session: AsyncSession) -> None:
    """A confirmed address goes in; the next person at that domain comes
    out as a single generated candidate instead of ten guesses.
    """
    repo = DomainPatternRepository(db_session)
    domain = f"{uuid.uuid4().hex[:8]}.example.com"

    learned = learn_from_confirmed(f"sarah.chen@{domain}", "Sarah Chen")
    assert learned is not None
    await repo.learn(learned.domain, learned.pattern, weight=learned.weight)
    await repo.learn(learned.domain, learned.pattern, weight=learned.weight)

    row = await repo.get(domain)
    assert row is not None

    blind = generate("Michael", "Torres", domain)
    targeted = generate(
        "Michael",
        "Torres",
        domain,
        known=KnownPattern(row.pattern, row.confirmed_count),
    )

    assert len(blind) > 1
    assert [c.value for c in targeted] == [f"michael.torres@{domain}"]


# --------------------------------------------------------------------------
# Session 09: pattern learning's read side, and weighted conflict
# resolution (a reply outweighs a website scrape).
# --------------------------------------------------------------------------


async def test_domain_pattern_get_returns_none_when_unknown(db_session: AsyncSession) -> None:
    repo = DomainPatternRepository(db_session)
    domain = f"{uuid.uuid4().hex[:8]}.example.com"
    assert await repo.get(domain) is None


async def test_learning_increments_confirmed_count(db_session: AsyncSession) -> None:
    repo = DomainPatternRepository(db_session)
    domain = f"{uuid.uuid4().hex[:8]}.example.com"

    first = await repo.learn(domain, "{first}.{last}", weight=1)
    assert first.confirmed_count == 1

    second = await repo.learn(domain, "{first}.{last}", weight=1)
    assert second.confirmed_count == 2

    fetched = await repo.get(domain)
    assert fetched is not None
    assert fetched.confirmed_count == 2


async def test_conflicting_pattern_keeps_higher_count(db_session: AsyncSession) -> None:
    """Session 09's weight-aware version of the Session 02 test above --
    a tie (equal weight) still keeps the incumbent, not the newer one.
    """
    repo = DomainPatternRepository(db_session)
    domain = f"{uuid.uuid4().hex[:8]}.example.com"

    await repo.learn(domain, "{first}.{last}", weight=2)
    conflicting = await repo.learn(domain, "{f}{last}", weight=2)  # tie -> incumbent wins

    assert conflicting.pattern == "{first}.{last}"
    assert conflicting.confirmed_count == 2


async def test_pattern_is_global_across_campaigns(db_session: AsyncSession) -> None:
    """Learning while working a lead in one campaign is visible reading
    the same domain from an unrelated campaign -- domain_patterns carries
    no campaign_id, by design (see the model's docstring).
    """
    repo = DomainPatternRepository(db_session)
    domain = f"{uuid.uuid4().hex[:8]}.example.com"

    await repo.learn(domain, "{first}.{last}", weight=1)  # "campaign A"
    seen_from_b = await repo.get(domain)  # "campaign B", same repository, same table

    assert seen_from_b is not None
    assert seen_from_b.pattern == "{first}.{last}"


async def test_reply_confirms_pattern_with_highest_weight(db_session: AsyncSession) -> None:
    """A reply actually received from an address (weight 3) can correct a
    pattern a weaker source (a website scrape, weight 1) got wrong -- the
    only way a poisoned domain recovers on its own.
    """
    from app.resolvers.email.learning import SOURCE_WEIGHTS, ConfirmationSource

    repo = DomainPatternRepository(db_session)
    domain = f"{uuid.uuid4().hex[:8]}.example.com"

    await repo.learn(
        domain, "{first}", weight=SOURCE_WEIGHTS[ConfirmationSource.WEBSITE]
    )  # a wrong guess from a website scrape
    corrected = await repo.learn(
        domain, "{first}.{last}", weight=SOURCE_WEIGHTS[ConfirmationSource.REPLY]
    )  # someone actually replied from this address

    assert corrected.pattern == "{first}.{last}"
    assert corrected.confirmed_count == SOURCE_WEIGHTS[ConfirmationSource.REPLY]


# --------------------------------------------------------------------------
# companies: country_code is NOT NULL — the compliance guard.
# --------------------------------------------------------------------------


async def test_company_requires_country_code(db_session: AsyncSession) -> None:
    campaign = Campaign(name="test campaign", icp={}, status=CampaignStatus.DRAFT)
    db_session.add(campaign)
    await db_session.flush()

    bad = Company(
        campaign_id=campaign.id,
        name="Acme Dental",
        country_code=None,  # type: ignore[arg-type]
    )
    db_session.add(bad)
    with pytest.raises(IntegrityError):
        await db_session.flush()


# --------------------------------------------------------------------------
# resolver_runs: a miss must be recordable — the anti-silent-failure guard.
# --------------------------------------------------------------------------


async def test_resolver_run_records_a_miss(db_session: AsyncSession) -> None:
    run = ResolverRun(
        resolver="serp_person",
        field="person_name",
        subject_id=uuid.uuid4(),
        tier=1,
        outcome=ResolverOutcome.MISS,
        candidates_returned=0,
        latency_ms=340,
        cost=Decimal("0"),
        ran_at=datetime.now(UTC),
    )
    db_session.add(run)
    await db_session.flush()

    fetched = await db_session.get(ResolverRun, run.id)
    assert fetched is not None
    assert fetched.outcome == ResolverOutcome.MISS
    assert fetched.candidates_returned == 0


# --------------------------------------------------------------------------
# email_addresses: UNKNOWN is a first-class, permanent verification state.
# --------------------------------------------------------------------------


async def test_email_unknown_is_a_valid_status(db_session: AsyncSession) -> None:
    campaign = Campaign(name="test campaign", icp={}, status=CampaignStatus.DRAFT)
    db_session.add(campaign)
    await db_session.flush()

    company = Company(campaign_id=campaign.id, name="Acme Dental", country_code="US")
    db_session.add(company)
    await db_session.flush()

    email = EmailAddress(
        company_id=company.id,
        address="jane@googleworkspacecompany.com",
        verify_status=VerifyStatus.UNKNOWN,
    )
    db_session.add(email)
    await db_session.flush()

    fetched = await db_session.get(EmailAddress, email.id)
    assert fetched is not None
    assert fetched.verify_status == VerifyStatus.UNKNOWN


# --------------------------------------------------------------------------
# hooks: Session 12's extended columns actually round-trip through Postgres.
# --------------------------------------------------------------------------


async def test_hook_repository_saves_a_found_hook(db_session: AsyncSession) -> None:
    campaign = Campaign(name="test campaign", icp={}, status=CampaignStatus.DRAFT)
    db_session.add(campaign)
    await db_session.flush()
    company = Company(campaign_id=campaign.id, name="Northgate Dental", country_code="US")
    db_session.add(company)
    await db_session.flush()

    repo = HookRepository(db_session)
    result = HookResult(
        status=HookVerdict.FOUND,
        hook_text="Northgate Dental opened a second location in Austin this March.",
        source_url="https://example.test/press/northgate-expands",
        event_date=date(2026, 3, 1),
        news_type=HookNewsType.NEW_LOCATION,
        channel=HookChannel.PRESS,
        confidence=HookConfidenceLevel.HIGH,
        swap_test_passed=True,
        needs_review=False,
        notes="",
        transcript="VERDICT: found\n...",
        model="deepseek/deepseek-v4-flash",
        cost_usd=0.031,
    )

    row = await repo.save(company.id, result)

    fetched = await db_session.get(Hook, row.id)
    assert fetched is not None
    assert fetched.status == HookVerdict.FOUND
    assert fetched.hook_text == result.hook_text
    assert fetched.source_url == result.source_url
    assert fetched.event_date == date(2026, 3, 1)
    assert fetched.swap_test_passed is True
    assert fetched.cost_usd == pytest.approx(0.031)


async def test_hook_repository_saves_a_blank_with_a_reason(db_session: AsyncSession) -> None:
    campaign = Campaign(name="test campaign", icp={}, status=CampaignStatus.DRAFT)
    db_session.add(campaign)
    await db_session.flush()
    company = Company(campaign_id=campaign.id, name="Quiet Clinic", country_code="US")
    db_session.add(company)
    await db_session.flush()

    repo = HookRepository(db_session)
    result = HookResult(
        status=HookVerdict.NONE_FOUND,
        hook_text=None,
        source_url=None,
        event_date=None,
        news_type=HookNewsType.NONE,
        channel=HookChannel.NONE,
        confidence=HookConfidenceLevel.LOW,
        swap_test_passed=False,
        needs_review=False,
        notes="no item cleared the CURRENT bar -- most recent find was 18 months old",
    )

    row = await repo.save(company.id, result)

    fetched = await db_session.get(Hook, row.id)
    assert fetched is not None
    assert fetched.status == HookVerdict.NONE_FOUND
    assert fetched.hook_text is None
    assert fetched.source_url is None
    # A blank must never be silent -- CLAUDE.md's whole "fail loud" ethos
    # applied to this table's one nullable-looking row.
    assert fetched.notes != ""
