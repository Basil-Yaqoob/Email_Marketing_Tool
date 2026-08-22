"""Integration tests for email resolution and verification.

The distinction these pin down is the one CLAUDE.md §10 is about: UNKNOWN
is a *conclusion*, not a failure. Google Workspace and Microsoft 365
accept mail for every address at a domain, so no probe can tell a real
mailbox from a typo there. Treating that as an error would abort runs and
misreport the funnel; treating it as VALID would send to addresses that
bounce. It is its own outcome, and everything downstream has to respect
that.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models.domain_pattern import DomainPattern
from app.db.models.enums import RoleClass, VerifyStatus
from app.db.models.fact import Fact
from app.db.repositories.campaign_repository import CampaignCreate, CampaignRepository
from app.db.repositories.company_repository import CompanyCreate, CompanyRepository
from app.db.repositories.domain_pattern_repository import DomainPatternRepository
from app.db.repositories.email_repository import (
    EmailAddressCreate,
    EmailAddressRepository,
)
from app.db.repositories.person_repository import PersonCreate, PersonRepository
from app.llm.cost import InMemoryCostTracker
from app.services.email_resolution import (
    RepositoryPatternStore,
    run_email_resolution,
    run_verification,
)
from app.services.factories import Runtime


class FakeDns:
    """Returns whatever MX hosts a test says. No network."""

    def __init__(self, hosts: dict[str, list[str]] | None = None) -> None:
        self._hosts = hosts or {}

    async def resolve_mx(self, domain: str) -> list[str]:
        return self._hosts.get(domain, [])


def _runtime() -> Runtime:
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


async def _lead(
    session: AsyncSession,
    *,
    domain: str = "riverside.example",
    person: str | None = "Jane Smith",
) -> tuple[uuid.UUID, uuid.UUID]:
    """A campaign with one company, optionally with a contact."""
    campaign = await CampaignRepository(session).create(CampaignCreate(name="c"))
    companies = CompanyRepository(session)
    await companies.bulk_upsert(
        [
            CompanyCreate(
                campaign_id=campaign.id,
                name="Riverside Dental",
                country_code="US",
                domain=domain,
                website=f"https://{domain}",
                source_place_id="p1",
            )
        ]
    )
    company = (await companies.list_for_campaign(campaign.id))[0]
    if person is not None:
        await PersonRepository(session).add(
            PersonCreate(company_id=company.id, full_name=person, role_class=RoleClass.OWNER)
        )
    return campaign.id, company.id


# ---------------------------------------------------------------------------
# Pattern generation
# ---------------------------------------------------------------------------


async def test_generates_candidate_addresses_with_provenance(
    db_session: AsyncSession,
) -> None:
    campaign_id, company_id = await _lead(db_session)

    result = await run_email_resolution(db_session, _runtime(), campaign_id)

    assert result.attempted == 1
    assert result.found == 1
    assert result.written > 1, "several ranked guesses when the domain format is unknown"

    addresses = await EmailAddressRepository(db_session).for_company(company_id)
    assert all(a.address.endswith("@riverside.example") for a in addresses)
    assert all(a.verify_status == VerifyStatus.UNCHECKED for a in addresses)

    facts = (await db_session.execute(select(Fact).where(Fact.field == "email"))).scalars().all()
    assert facts, "every generated address is recorded as a fact"
    assert all(f.source == "email_pattern" for f in facts)


async def test_a_known_domain_pattern_collapses_the_guesses(
    db_session: AsyncSession,
) -> None:
    """The whole economic point of the pattern store: one confirmed
    address turns nine future guesses at that domain into one.
    """
    campaign_id, company_id = await _lead(db_session)
    await DomainPatternRepository(db_session).learn(
        "riverside.example", "{first}.{last}", weight=5
    )

    result = await run_email_resolution(db_session, _runtime(), campaign_id)

    addresses = await EmailAddressRepository(db_session).for_company(company_id)
    assert result.written == 1, "a known format yields exactly one address"
    assert addresses[0].address == "jane.smith@riverside.example"


async def test_a_crawled_address_is_never_replaced_by_a_guess(
    db_session: AsyncSession,
) -> None:
    """A published address beats any generated one. Overwriting it with a
    guess would trade a certainty for a hypothesis.
    """
    campaign_id, company_id = await _lead(db_session)
    await EmailAddressRepository(db_session).add(
        EmailAddressCreate(company_id=company_id, address="hello@riverside.example")
    )

    result = await run_email_resolution(db_session, _runtime(), campaign_id)

    addresses = await EmailAddressRepository(db_session).for_company(company_id)
    assert result.attempted == 0
    assert [a.address for a in addresses] == ["hello@riverside.example"]


async def test_no_person_means_nothing_to_generate(db_session: AsyncSession) -> None:
    """Not a failure — a pattern needs a name to render."""
    campaign_id, _ = await _lead(db_session, person=None)

    result = await run_email_resolution(db_session, _runtime(), campaign_id)

    assert result.attempted == 0
    assert result.written == 0


async def test_no_domain_means_nothing_to_generate(db_session: AsyncSession) -> None:
    campaign = await CampaignRepository(db_session).create(CampaignCreate(name="c"))
    companies = CompanyRepository(db_session)
    await companies.bulk_upsert(
        [
            CompanyCreate(
                campaign_id=campaign.id,
                name="No Domain Dental",
                country_code="US",
                domain=None,
                source_place_id="p1",
            )
        ]
    )
    company = (await companies.list_for_campaign(campaign.id))[0]
    await PersonRepository(db_session).add(
        PersonCreate(company_id=company.id, full_name="Jane Smith")
    )

    result = await run_email_resolution(db_session, _runtime(), campaign.id)

    assert result.attempted == 0


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


async def test_google_hosted_domains_resolve_to_unknown_not_error(
    db_session: AsyncSession,
) -> None:
    """The §10 ceiling, enforced rather than papered over.

    Google accepts mail for every address at the domain, so a probe cannot
    distinguish a real mailbox from a typo. That is UNKNOWN — a conclusion
    the ladder reached — and it must not be counted as an error, or every
    Workspace-heavy campaign would abort.
    """
    campaign_id, company_id = await _lead(db_session)
    await EmailAddressRepository(db_session).add(
        EmailAddressCreate(company_id=company_id, address="jane@riverside.example")
    )
    dns = FakeDns({"riverside.example": ["aspmx.l.google.com"]})

    result = await run_verification(db_session, _runtime(), campaign_id, dns=dns)

    assert len(result.errors) == 0, "UNKNOWN is not an error"

    rows = await EmailAddressRepository(db_session).for_company(company_id)
    assert rows[0].verify_status == VerifyStatus.UNKNOWN
    assert rows[0].verified_at is not None, "a conclusion is timestamped"


async def test_a_domain_with_no_mx_is_invalid(db_session: AsyncSession) -> None:
    """No MX means the domain accepts no mail at all — a real answer, and
    the one case the free tier can settle definitively.
    """
    campaign_id, company_id = await _lead(db_session)
    await EmailAddressRepository(db_session).add(
        EmailAddressCreate(company_id=company_id, address="jane@riverside.example")
    )

    await run_verification(db_session, _runtime(), campaign_id, dns=FakeDns({}))

    rows = await EmailAddressRepository(db_session).for_company(company_id)
    assert rows[0].verify_status == VerifyStatus.INVALID


async def test_verified_addresses_are_not_rechecked(db_session: AsyncSession) -> None:
    """Re-running verification must not re-probe addresses the ladder has
    already settled — including UNKNOWN ones, whose answer cannot change.
    """
    campaign_id, company_id = await _lead(db_session)
    emails = EmailAddressRepository(db_session)
    await emails.add(
        EmailAddressCreate(company_id=company_id, address="jane@riverside.example")
    )
    dns = FakeDns({"riverside.example": ["aspmx.l.google.com"]})

    first = await run_verification(db_session, _runtime(), campaign_id, dns=dns)
    second = await run_verification(db_session, _runtime(), campaign_id, dns=dns)

    assert first.attempted == 1
    assert second.attempted == 0, "already-settled addresses are not re-queued"


async def test_nothing_pending_is_not_a_failure(db_session: AsyncSession) -> None:
    campaign_id, _ = await _lead(db_session)

    result = await run_verification(db_session, _runtime(), campaign_id, dns=FakeDns({}))

    assert result.attempted == 0
    assert result.error_rate == 0.0


async def test_missing_campaign_raises(db_session: AsyncSession) -> None:
    runtime = _runtime()
    with pytest.raises(ValueError, match="does not exist"):
        await run_email_resolution(db_session, runtime, uuid.uuid4())
    with pytest.raises(ValueError, match="does not exist"):
        await run_verification(db_session, runtime, uuid.uuid4(), dns=FakeDns({}))


# ---------------------------------------------------------------------------
# The store adapter
# ---------------------------------------------------------------------------


async def test_pattern_store_adapter_converts_the_row(db_session: AsyncSession) -> None:
    """The resolver must never receive a SQLAlchemy model (CLAUDE.md §6)."""
    repository = DomainPatternRepository(db_session)
    await repository.learn("example.com", "{first}", weight=3)

    store = RepositoryPatternStore(repository)
    known = await store.get("example.com")

    assert known is not None
    assert not isinstance(known, DomainPattern)
    assert known.pattern == "{first}"
    assert known.confirmed_count == 3
    assert await store.get("unknown.example") is None
