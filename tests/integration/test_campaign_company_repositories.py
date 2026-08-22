"""Integration tests for CampaignRepository and CompanyRepository.

These two are what the campaigns and leads pages 500'd on: 14 tables
existed but nothing could read or write the two the UI opens with.

The load-bearing tests here are the funnel arithmetic (the number a user
actually looks at, which must not exceed what was discovered) and
cross-campaign discovery isolation (the bug migration 7ed091f9c1fc fixed,
where the first campaign to find a business permanently claimed it).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.email import EmailAddress
from app.db.models.enums import (
    CampaignStatus,
    MailboxProvider,
    MessageStatus,
    SendStatus,
    VerifyStatus,
)
from app.db.models.mailbox import Mailbox
from app.db.models.message import Message
from app.db.models.person import Person
from app.db.models.reply import Reply
from app.db.models.send import Send
from app.db.repositories.campaign_repository import CampaignCreate, CampaignRepository
from app.db.repositories.company_repository import CompanyCreate, CompanyRepository


async def _campaign(session: AsyncSession, name: str = "Test Campaign") -> uuid.UUID:
    repo = CampaignRepository(session)
    campaign = await repo.create(
        CampaignCreate(name=name, icp={"categories": ["dentist"]}, jurisdiction="US")
    )
    return campaign.id


def _company(campaign_id: uuid.UUID, **overrides: object) -> CompanyCreate:
    defaults: dict[str, object] = {
        "campaign_id": campaign_id,
        "name": "Riverside Dental",
        "country_code": "US",
        "website": "https://riverside.example",
        "source_place_id": "place-1",
    }
    defaults.update(overrides)
    return CompanyCreate(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# CampaignRepository
# ---------------------------------------------------------------------------


async def test_create_and_get_campaign(db_session: AsyncSession) -> None:
    repo = CampaignRepository(db_session)
    created = await repo.create(
        CampaignCreate(name="Austin Dentists", icp={"categories": ["dentist"]}, jurisdiction="US")
    )

    fetched = await repo.get(created.id)
    assert fetched is not None
    assert fetched.name == "Austin Dentists"
    assert fetched.icp == {"categories": ["dentist"]}
    assert fetched.status == CampaignStatus.DRAFT, "new campaigns start in DRAFT"


async def test_list_returns_newest_first(db_session: AsyncSession) -> None:
    """Timestamps are set explicitly here on purpose.

    Postgres `now()` is transaction time, so three rows created in one test
    transaction share a `created_at` and their relative order would be
    decided by the id tiebreak, testing nothing about recency.
    """
    repo = CampaignRepository(db_session)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for offset, name in enumerate(("first", "second", "third")):
        campaign = await repo.create(CampaignCreate(name=name))
        campaign.created_at = base.replace(day=1 + offset)
    await db_session.flush()

    listed = await repo.list()

    assert [c.name for c in listed[:3]] == ["third", "second", "first"]


async def test_pagination_is_deterministic_when_timestamps_tie(
    db_session: AsyncSession,
) -> None:
    """bulk_upsert writes every company inside one transaction, so they all
    share a `created_at`. Ordering by that column alone leaves LIMIT/OFFSET
    free to reorder between queries, which shows up as a lead appearing
    twice on page 2 while another vanishes. The id tiebreak prevents it.
    """
    campaign_id = await _campaign(db_session)
    repo = CompanyRepository(db_session)
    await repo.bulk_upsert(
        [_company(campaign_id, name=f"c{i}", source_place_id=f"p{i}") for i in range(20)]
    )

    def _ids(rows: list[object]) -> list[uuid.UUID]:
        return [r.id for r in rows]  # type: ignore[attr-defined]

    pages = [
        _ids(await repo.list_for_campaign(campaign_id, limit=5, offset=offset))
        for offset in (0, 5, 10, 15)
    ]
    seen = [company_id for page in pages for company_id in page]

    assert len(seen) == 20
    assert len(set(seen)) == 20, "a row was repeated or dropped across pages"

    # And the same query twice returns the same order.
    again = _ids(await repo.list_for_campaign(campaign_id, limit=5, offset=5))
    assert again == pages[1]


async def test_update_leaves_omitted_fields_alone(db_session: AsyncSession) -> None:
    """A caller renaming a campaign must not blank its ICP."""
    repo = CampaignRepository(db_session)
    created = await repo.create(
        CampaignCreate(name="Old", icp={"categories": ["dentist"]}, jurisdiction="US")
    )

    updated = await repo.update(created.id, name="New")

    assert updated.name == "New"
    assert updated.icp == {"categories": ["dentist"]}, "ICP survived a name-only edit"
    assert updated.jurisdiction == "US"


async def test_set_status_and_missing_campaign_raises(db_session: AsyncSession) -> None:
    repo = CampaignRepository(db_session)
    created = await repo.create(CampaignCreate(name="c"))

    updated = await repo.set_status(created.id, CampaignStatus.DISCOVERING)
    assert updated.status == CampaignStatus.DISCOVERING

    # Fails loudly rather than returning a sentinel (CLAUDE.md 2.1).
    with pytest.raises(ValueError, match="does not exist"):
        await repo.set_status(uuid.uuid4(), CampaignStatus.PAUSED)


async def test_funnel_of_empty_campaign_is_all_zero(db_session: AsyncSession) -> None:
    """An empty funnel is a real, expected state — not an error, and not
    a crash on a campaign nothing has run against yet.
    """
    campaign_id = await _campaign(db_session)
    funnel = await CampaignRepository(db_session).funnel(campaign_id)

    assert funnel.discovered == 0
    assert funnel.sent == 0
    assert funnel.replied == 0


async def test_funnel_counts_companies_not_rows(db_session: AsyncSession) -> None:
    """The middle stages count companies that converted, not child rows.

    One company with three contacts is one company that yielded a person.
    Counting rows would push person_found above discovered and render a
    funnel bar wider than 100%.
    """
    campaign_id = await _campaign(db_session)
    companies = CompanyRepository(db_session)
    await companies.bulk_upsert(
        [
            _company(campaign_id, name="A", source_place_id="a"),
            _company(campaign_id, name="B", source_place_id="b", website=None),
        ]
    )
    rows = await companies.list_for_campaign(campaign_id)
    company_a = next(c for c in rows if c.name == "A")

    # Three people and two emails, all on the same single company.
    for full_name in ("P1", "P2", "P3"):
        db_session.add(Person(company_id=company_a.id, full_name=full_name))
    for address in ("x@a.example", "y@a.example"):
        db_session.add(EmailAddress(company_id=company_a.id, address=address))
    await db_session.flush()

    funnel = await CampaignRepository(db_session).funnel(campaign_id)

    assert funnel.discovered == 2
    assert funnel.with_website == 1, "company B has no website"
    assert funnel.person_found == 1, "3 people on 1 company is 1 company, not 3"
    assert funnel.email_found == 1, "2 emails on 1 company is 1 company, not 2"
    assert funnel.person_found <= funnel.discovered


async def test_funnel_verified_counts_only_valid(db_session: AsyncSession) -> None:
    """UNKNOWN is not verified. It is also not a failure — it just does
    not clear this particular bar (CLAUDE.md §10).
    """
    campaign_id = await _campaign(db_session)
    companies = CompanyRepository(db_session)
    await companies.bulk_upsert(
        [
            _company(campaign_id, name="Valid", source_place_id="v"),
            _company(campaign_id, name="Unknown", source_place_id="u"),
        ]
    )
    rows = {c.name: c for c in await companies.list_for_campaign(campaign_id)}

    db_session.add(
        EmailAddress(
            company_id=rows["Valid"].id,
            address="ok@valid.example",
            verify_status=VerifyStatus.VALID,
        )
    )
    db_session.add(
        EmailAddress(
            company_id=rows["Unknown"].id,
            address="maybe@unknown.example",
            verify_status=VerifyStatus.UNKNOWN,
        )
    )
    await db_session.flush()

    funnel = await CampaignRepository(db_session).funnel(campaign_id)

    assert funnel.email_found == 2
    assert funnel.verified == 1


async def test_funnel_counts_sends_and_replies(db_session: AsyncSession) -> None:
    campaign_id = await _campaign(db_session)
    companies = CompanyRepository(db_session)
    await companies.bulk_upsert([_company(campaign_id, source_place_id="s")])
    company = (await companies.list_for_campaign(campaign_id))[0]

    mailbox = Mailbox(
        provider=MailboxProvider.GENERIC_SMTP,
        email_address="sender@example.com",
        encrypted_credentials=b"ciphertext",
    )
    message = Message(
        company_id=company.id,
        angle="news_hook",
        subject="s",
        body="b",
        status=MessageStatus.APPROVED,
    )
    db_session.add_all([mailbox, message])
    await db_session.flush()

    sent = Send(message_id=message.id, mailbox_id=mailbox.id, status=SendStatus.SENT)
    queued = Send(message_id=message.id, mailbox_id=mailbox.id, status=SendStatus.QUEUED)
    db_session.add_all([sent, queued])
    await db_session.flush()

    db_session.add(
        Reply(send_id=sent.id, received_at=datetime.now(UTC), raw_snippet="sure, tell me more")
    )
    await db_session.flush()

    repo = CampaignRepository(db_session)
    funnel = await repo.funnel(campaign_id)

    assert funnel.drafted == 1
    assert funnel.sent == 1, "a QUEUED send has not been sent"
    assert funnel.replied == 1
    assert await repo.approved_message_count(campaign_id) == 1


async def test_funnel_is_isolated_per_campaign(db_session: AsyncSession) -> None:
    """One campaign's numbers must never bleed into another's."""
    first = await _campaign(db_session, "first")
    second = await _campaign(db_session, "second")
    companies = CompanyRepository(db_session)

    await companies.bulk_upsert(
        [_company(first, name=f"c{i}", source_place_id=f"f{i}") for i in range(3)]
    )
    await companies.bulk_upsert([_company(second, name="only", source_place_id="s0")])

    repo = CampaignRepository(db_session)
    assert (await repo.funnel(first)).discovered == 3
    assert (await repo.funnel(second)).discovered == 1


# ---------------------------------------------------------------------------
# CompanyRepository
# ---------------------------------------------------------------------------


async def test_bulk_upsert_inserts_and_counts(db_session: AsyncSession) -> None:
    campaign_id = await _campaign(db_session)
    repo = CompanyRepository(db_session)

    result = await repo.bulk_upsert(
        [_company(campaign_id, name=f"c{i}", source_place_id=f"p{i}") for i in range(5)]
    )

    assert result.inserted == 5
    assert result.skipped == 0
    assert result.submitted == 5
    assert await repo.count(campaign_id) == 5


async def test_bulk_upsert_of_empty_list_is_a_noop(db_session: AsyncSession) -> None:
    repo = CompanyRepository(db_session)
    result = await repo.bulk_upsert([])
    assert result.inserted == 0
    assert result.skipped == 0


async def test_rerunning_discovery_is_idempotent(db_session: AsyncSession) -> None:
    """Re-running discovery must not duplicate companies, and the skip is
    reported as a normal outcome rather than swallowed.
    """
    campaign_id = await _campaign(db_session)
    repo = CompanyRepository(db_session)
    batch = [_company(campaign_id, name=f"c{i}", source_place_id=f"p{i}") for i in range(3)]

    first = await repo.bulk_upsert(batch)
    second = await repo.bulk_upsert(batch)

    assert first.inserted == 3
    assert second.inserted == 0
    assert second.skipped == 3
    assert await repo.count(campaign_id) == 3


async def test_two_campaigns_can_discover_the_same_business(db_session: AsyncSession) -> None:
    """Regression for migration 7ed091f9c1fc.

    source_place_id was UNIQUE globally while the row carried a NOT NULL
    campaign_id. The first campaign to discover a business claimed its
    place id forever, so any later campaign covering the same area lost
    that lead to a constraint violation. With overlapping campaigns — the
    whole point of running several — leads went silently missing.
    """
    first = await _campaign(db_session, "first")
    second = await _campaign(db_session, "second")
    repo = CompanyRepository(db_session)

    shared_place = "shared-place-id"
    a = await repo.bulk_upsert([_company(first, source_place_id=shared_place)])
    b = await repo.bulk_upsert([_company(second, source_place_id=shared_place)])

    assert a.inserted == 1
    assert b.inserted == 1, "second campaign must get its own row for the same business"
    assert await repo.count(first) == 1
    assert await repo.count(second) == 1


async def test_null_place_id_never_dedupes(db_session: AsyncSession) -> None:
    """NULL != NULL in SQL, so sources with no stable id always insert.
    Fuzzy dedup for those is discovery's job, not the database's.
    """
    campaign_id = await _campaign(db_session)
    repo = CompanyRepository(db_session)

    result = await repo.bulk_upsert(
        [_company(campaign_id, name="No Id", source_place_id=None) for _ in range(3)]
    )

    assert result.inserted == 3
    assert result.skipped == 0


async def test_with_website_excludes_companies_that_have_none(db_session: AsyncSession) -> None:
    """A company with no website is not a crawl failure — there is nothing
    to fetch, so it never reaches the crawler.
    """
    campaign_id = await _campaign(db_session)
    repo = CompanyRepository(db_session)
    await repo.bulk_upsert(
        [
            _company(campaign_id, name="has", source_place_id="h"),
            _company(campaign_id, name="none", source_place_id="n", website=None),
        ]
    )

    crawlable = await repo.with_website(campaign_id)

    assert [c.name for c in crawlable] == ["has"]


async def test_list_paginates_and_filters(db_session: AsyncSession) -> None:
    campaign_id = await _campaign(db_session)
    repo = CompanyRepository(db_session)
    await repo.bulk_upsert(
        [_company(campaign_id, name=f"c{i}", source_place_id=f"p{i}") for i in range(10)]
    )

    page = await repo.list_for_campaign(campaign_id, limit=4, offset=0)
    next_page = await repo.list_for_campaign(campaign_id, limit=4, offset=4)

    assert len(page) == 4
    assert len(next_page) == 4
    assert {c.id for c in page}.isdisjoint({c.id for c in next_page})
