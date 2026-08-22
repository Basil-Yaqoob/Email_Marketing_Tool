"""Integration tests for the remaining eight repositories.

Concentrates on the decisions that are easy to get quietly wrong:
  - UNKNOWN emails are never re-queued for verification (they are answered,
    not unanswered — CLAUDE.md §10)
  - mailbox credentials never leave the repository except through the one
    method named for it
  - suppression matches domains and is case-insensitive
  - a FAILED send does not consume a mailbox's daily cap
  - misses and errors are recorded as telemetry, not just hits
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.enums import (
    MailboxProvider,
    ReplyClassification,
    ResolverOutcome,
    RoleClass,
    SeedPlacement,
    SendStatus,
    VerifyStatus,
)
from app.db.models.message import Message
from app.db.models.resolver_run import ResolverRun
from app.db.repositories.campaign_repository import CampaignCreate, CampaignRepository
from app.db.repositories.company_repository import CompanyCreate, CompanyRepository
from app.db.repositories.email_repository import (
    EmailAddressCreate,
    EmailAddressRepository,
)
from app.db.repositories.mailbox_repository import (
    MailboxCreate,
    MailboxCredentials,
    MailboxRepository,
)
from app.db.repositories.person_repository import PersonCreate, PersonRepository
from app.db.repositories.reply_repository import ReplyCreate, ReplyRepository
from app.db.repositories.resolver_run_repository import (
    ResolverRunCreate,
    ResolverRunRepository,
)
from app.db.repositories.seed_test_repository import SeedTestCreate, SeedTestRepository
from app.db.repositories.send_repository import SendCreate, SendRepository
from app.db.repositories.suppression_repository import (
    SuppressionCreate,
    SuppressionRepository,
)
from app.sending.credentials import CredentialVault


async def _company(session: AsyncSession) -> uuid.UUID:
    campaign = await CampaignRepository(session).create(CampaignCreate(name="c"))
    repo = CompanyRepository(session)
    await repo.bulk_upsert(
        [
            CompanyCreate(
                campaign_id=campaign.id,
                name="Riverside Dental",
                country_code="US",
                website="https://riverside.example",
                source_place_id="p1",
            )
        ]
    )
    return (await repo.list_for_campaign(campaign.id))[0].id


def _vault() -> CredentialVault:
    return CredentialVault(Fernet.generate_key())


def _credentials(**overrides: object) -> MailboxCredentials:
    defaults: dict[str, object] = {
        "smtp_host": "smtp.example.com",
        "smtp_username": "sender@example.com",
        "smtp_password": "hunter2-the-real-password",
        "imap_host": "imap.example.com",
        "imap_username": "sender@example.com",
        "imap_password": "hunter2-the-real-password",
    }
    defaults.update(overrides)
    return MailboxCredentials(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Person
# ---------------------------------------------------------------------------


async def test_best_for_company_prefers_decision_makers(db_session: AsyncSession) -> None:
    """An OWNER outranks a MARKETING contact. The product is outreach to
    decision makers; writing to whoever was found first would undercut the
    one number that matters (§10's 55-70% decision-maker coverage).
    """
    company_id = await _company(db_session)
    repo = PersonRepository(db_session)
    await repo.bulk_add(
        [
            PersonCreate(
                company_id=company_id, full_name="Marketing Mike", role_class=RoleClass.MARKETING
            ),
            PersonCreate(
                company_id=company_id, full_name="Owner Olive", role_class=RoleClass.OWNER
            ),
            PersonCreate(company_id=company_id, full_name="Random Ray", role_class=RoleClass.OTHER),
        ]
    )

    best = await repo.best_for_company(company_id)

    assert best is not None
    assert best.full_name == "Owner Olive"
    ordered = await repo.for_company(company_id)
    assert [p.full_name for p in ordered] == ["Owner Olive", "Marketing Mike", "Random Ray"]


async def test_best_for_company_with_no_people_is_none(db_session: AsyncSession) -> None:
    """No contact found is an expected outcome, not an error."""
    company_id = await _company(db_session)
    assert await PersonRepository(db_session).best_for_company(company_id) is None


# ---------------------------------------------------------------------------
# EmailAddress
# ---------------------------------------------------------------------------


async def test_unknown_emails_are_never_requeued_for_verification(
    db_session: AsyncSession,
) -> None:
    """UNKNOWN means 'verified as unverifiable' — Google/Microsoft hosted
    or catch-all. Re-queuing those would burn SMTP probes forever on an
    answer that cannot change, and make the funnel look like verification
    was failing when it was working exactly as designed (§10).
    """
    company_id = await _company(db_session)
    campaign_id = (await CompanyRepository(db_session).get(company_id)).campaign_id  # type: ignore[union-attr]
    repo = EmailAddressRepository(db_session)
    await repo.bulk_add(
        [
            EmailAddressCreate(company_id=company_id, address="new@x.example"),
            EmailAddressCreate(
                company_id=company_id,
                address="unknown@x.example",
                verify_status=VerifyStatus.UNKNOWN,
            ),
            EmailAddressCreate(
                company_id=company_id, address="valid@x.example", verify_status=VerifyStatus.VALID
            ),
            EmailAddressCreate(
                company_id=company_id,
                address="catchall@x.example",
                verify_status=VerifyStatus.CATCH_ALL,
            ),
        ]
    )

    pending = await repo.pending_verification(campaign_id)

    assert [e.address for e in pending] == ["new@x.example"]


async def test_set_verify_status_stamps_verified_at(db_session: AsyncSession) -> None:
    company_id = await _company(db_session)
    repo = EmailAddressRepository(db_session)
    row = await repo.add(EmailAddressCreate(company_id=company_id, address="a@x.example"))
    assert row.verified_at is None

    updated = await repo.set_verify_status(row.id, VerifyStatus.UNKNOWN)

    assert updated.verify_status == VerifyStatus.UNKNOWN
    assert updated.verified_at is not None, "UNKNOWN is a conclusion, so it is timestamped"

    with pytest.raises(ValueError, match="does not exist"):
        await repo.set_verify_status(uuid.uuid4(), VerifyStatus.VALID)


async def test_status_breakdown_groups_by_status(db_session: AsyncSession) -> None:
    company_id = await _company(db_session)
    campaign_id = (await CompanyRepository(db_session).get(company_id)).campaign_id  # type: ignore[union-attr]
    repo = EmailAddressRepository(db_session)
    await repo.bulk_add(
        [
            EmailAddressCreate(
                company_id=company_id, address="v1@x.example", verify_status=VerifyStatus.VALID
            ),
            EmailAddressCreate(
                company_id=company_id, address="v2@x.example", verify_status=VerifyStatus.VALID
            ),
            EmailAddressCreate(
                company_id=company_id, address="u@x.example", verify_status=VerifyStatus.UNKNOWN
            ),
        ]
    )

    breakdown = await repo.status_breakdown(campaign_id)

    assert breakdown["valid"] == 2
    assert breakdown["unknown"] == 1


# ---------------------------------------------------------------------------
# Mailbox — the credential boundary
# ---------------------------------------------------------------------------


async def test_mailbox_views_never_carry_credentials(db_session: AsyncSession) -> None:
    """The safe projection has no credentials field at all, so a template
    or API response physically cannot render one (CLAUDE.md §2.4).
    """
    repo = MailboxRepository(db_session, _vault())
    view = await repo.add(
        MailboxCreate(
            provider=MailboxProvider.GENERIC_SMTP,
            email_address="sender@example.com",
            credentials=_credentials(),
        )
    )

    assert not hasattr(view, "credentials")
    assert not hasattr(view, "encrypted_credentials")
    blob = view.model_dump_json().lower()
    assert "hunter2" not in blob
    assert "password" not in blob

    listed = await repo.list()
    assert "hunter2" not in listed[0].model_dump_json()


async def test_credentials_round_trip_only_through_the_named_method(
    db_session: AsyncSession,
) -> None:
    vault = _vault()
    repo = MailboxRepository(db_session, vault)
    view = await repo.add(
        MailboxCreate(
            provider=MailboxProvider.GOOGLE_WORKSPACE,
            email_address="sender@example.com",
            credentials=_credentials(),
        )
    )

    recovered = await repo.credentials_for(view.id)

    assert recovered.smtp_host == "smtp.example.com"
    assert recovered.smtp_password.get_secret_value() == "hunter2-the-real-password"
    assert recovered.imap_password is not None
    # And a repr of the credentials still redacts, in case one is logged.
    assert "hunter2" not in repr(recovered)


async def test_credentials_are_encrypted_at_rest(db_session: AsyncSession) -> None:
    """The stored bytes must not contain the plaintext password."""
    from app.db.models.mailbox import Mailbox

    repo = MailboxRepository(db_session, _vault())
    view = await repo.add(
        MailboxCreate(
            provider=MailboxProvider.GENERIC_SMTP,
            email_address="sender@example.com",
            credentials=_credentials(),
        )
    )

    row = (await db_session.execute(select(Mailbox).where(Mailbox.id == view.id))).scalar_one()

    assert b"hunter2" not in row.encrypted_credentials
    assert b"smtp.example.com" not in row.encrypted_credentials


async def test_warmup_stage_and_cap_move_together(db_session: AsyncSession) -> None:
    """A stage without its matching cap would let a barely-warm mailbox
    send at full rate.
    """
    repo = MailboxRepository(db_session, _vault())
    view = await repo.add(
        MailboxCreate(
            provider=MailboxProvider.GENERIC_SMTP,
            email_address="sender@example.com",
            credentials=_credentials(),
        )
    )

    updated = await repo.set_warmup_stage(view.id, stage=3, cap=40)

    assert updated.warmup_stage == 3
    assert updated.daily_cap == 40


# ---------------------------------------------------------------------------
# Suppression
# ---------------------------------------------------------------------------


async def test_suppression_matches_domain_and_ignores_case(db_session: AsyncSession) -> None:
    repo = SuppressionRepository(db_session)
    await repo.add(SuppressionCreate(value="blocked@example.com", reason="unsubscribed"))
    await repo.add(SuppressionCreate(value="nope.example", reason="domain complaint"))

    assert await repo.is_suppressed("blocked@example.com")
    assert await repo.is_suppressed("BLOCKED@Example.COM"), "matching must be case-insensitive"
    assert await repo.is_suppressed("anyone@nope.example"), "domain suppression covers all"
    assert not await repo.is_suppressed("fine@example.com")


async def test_suppressing_twice_is_not_an_error(db_session: AsyncSession) -> None:
    """Someone unsubscribing twice is normal behaviour."""
    repo = SuppressionRepository(db_session)
    await repo.add(SuppressionCreate(value="dup@example.com", reason="unsubscribed"))
    await repo.add(SuppressionCreate(value="dup@example.com", reason="unsubscribed again"))

    assert await repo.count() == 1


async def test_filter_allowed_removes_suppressed_recipients(db_session: AsyncSession) -> None:
    repo = SuppressionRepository(db_session)
    await repo.add(SuppressionCreate(value="no@example.com", reason="unsubscribed"))
    await repo.add(SuppressionCreate(value="banned.example", reason="domain"))

    allowed = await repo.filter_allowed(
        ["yes@example.com", "no@example.com", "someone@banned.example", "OK@Example.com"]
    )

    assert allowed == ["yes@example.com", "OK@Example.com"]


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------


async def _message_and_mailbox(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    company_id = await _company(session)
    message = Message(company_id=company_id, angle="news_hook", subject="s", body="b")
    session.add(message)
    mailbox = await MailboxRepository(session, _vault()).add(
        MailboxCreate(
            provider=MailboxProvider.GENERIC_SMTP,
            email_address="sender@example.com",
            credentials=_credentials(),
        )
    )
    await session.flush()
    return message.id, mailbox.id


async def test_failed_sends_do_not_consume_the_daily_cap(db_session: AsyncSession) -> None:
    """A FAILED send never left the mailbox. Counting it would let a run
    of transient SMTP errors silently throttle a healthy mailbox to zero.
    """
    message_id, mailbox_id = await _message_and_mailbox(db_session)
    repo = SendRepository(db_session)

    sent = await repo.create(SendCreate(message_id=message_id, mailbox_id=mailbox_id))
    await repo.mark_sent(sent.id, message_id_header="<a@example.com>")

    failed = await repo.create(SendCreate(message_id=message_id, mailbox_id=mailbox_id))
    await repo.mark_failed(failed.id, SendStatus.FAILED)

    # QUEUED has not been attempted either.
    await repo.create(SendCreate(message_id=message_id, mailbox_id=mailbox_id))

    assert await repo.sent_today(mailbox_id) == 1


async def test_bounced_sends_do_consume_the_cap(db_session: AsyncSession) -> None:
    """A bounce means the message did leave and did hit the recipient's
    server — it cost reputation, so it costs quota.
    """
    message_id, mailbox_id = await _message_and_mailbox(db_session)
    repo = SendRepository(db_session)

    bounced = await repo.create(SendCreate(message_id=message_id, mailbox_id=mailbox_id))
    await repo.mark_sent(bounced.id, message_id_header="<b@example.com>")
    await repo.mark_failed(bounced.id, SendStatus.BOUNCED)

    assert await repo.sent_today(mailbox_id) == 1


async def test_sent_today_is_scoped_to_the_day(db_session: AsyncSession) -> None:
    message_id, mailbox_id = await _message_and_mailbox(db_session)
    repo = SendRepository(db_session)

    yesterday = await repo.create(SendCreate(message_id=message_id, mailbox_id=mailbox_id))
    await repo.mark_sent(
        yesterday.id,
        message_id_header="<old@example.com>",
        sent_at=datetime.now(UTC) - timedelta(days=1),
    )

    assert await repo.sent_today(mailbox_id) == 0


async def test_send_lookup_by_message_id_header(db_session: AsyncSession) -> None:
    """How an inbound reply is threaded back to the send that caused it."""
    message_id, mailbox_id = await _message_and_mailbox(db_session)
    repo = SendRepository(db_session)
    send = await repo.create(SendCreate(message_id=message_id, mailbox_id=mailbox_id))
    await repo.mark_sent(send.id, message_id_header="<thread-me@example.com>")

    found = await repo.by_message_id_header("<thread-me@example.com>")

    assert found is not None
    assert found.id == send.id
    assert await repo.by_message_id_header("<never-sent@example.com>") is None


async def test_mark_failed_rejects_a_success_status(db_session: AsyncSession) -> None:
    message_id, mailbox_id = await _message_and_mailbox(db_session)
    repo = SendRepository(db_session)
    send = await repo.create(SendCreate(message_id=message_id, mailbox_id=mailbox_id))

    with pytest.raises(ValueError, match="not a failure status"):
        await repo.mark_failed(send.id, SendStatus.SENT)


# ---------------------------------------------------------------------------
# Reply
# ---------------------------------------------------------------------------


async def test_reply_rate_of_an_unused_mailbox_is_zero_not_a_crash(
    db_session: AsyncSession,
) -> None:
    """0-of-0 is 'no signal yet', not 'performing badly' — and definitely
    not a ZeroDivisionError.
    """
    _, mailbox_id = await _message_and_mailbox(db_session)
    assert await ReplyRepository(db_session).reply_rate(mailbox_id) == 0.0


async def test_reply_rate_counts_replies_per_delivered_send(
    db_session: AsyncSession,
) -> None:
    message_id, mailbox_id = await _message_and_mailbox(db_session)
    sends = SendRepository(db_session)
    replies = ReplyRepository(db_session)

    for i in range(4):
        send = await sends.create(SendCreate(message_id=message_id, mailbox_id=mailbox_id))
        await sends.mark_sent(send.id, message_id_header=f"<{i}@example.com>")
        if i < 1:
            await replies.record(ReplyCreate(send_id=send.id, raw_snippet="interested"))

    assert await replies.reply_rate(mailbox_id) == 0.25


async def test_unclassified_replies_are_queued_then_classified(
    db_session: AsyncSession,
) -> None:
    message_id, mailbox_id = await _message_and_mailbox(db_session)
    send = await SendRepository(db_session).create(
        SendCreate(message_id=message_id, mailbox_id=mailbox_id)
    )
    repo = ReplyRepository(db_session)
    reply = await repo.record(ReplyCreate(send_id=send.id, raw_snippet="not interested"))

    assert [r.id for r in await repo.unclassified()] == [reply.id]

    await repo.classify(reply.id, ReplyClassification.NOT_INTERESTED)

    assert await repo.unclassified() == []


# ---------------------------------------------------------------------------
# ResolverRun — the anti-silent-failure telemetry
# ---------------------------------------------------------------------------


async def test_misses_and_errors_are_recorded_not_just_hits(
    db_session: AsyncSession,
) -> None:
    """The prototype logged 1,640 consecutive failures and exited 0. A
    resolver returning nothing 1,000 times is exactly the signal worth
    surfacing, and it is invisible if only successes are written.
    """
    repo = ResolverRunRepository(db_session)
    written = await repo.record_many(
        [
            ResolverRunCreate(
                resolver="osm",
                field="company",
                tier=0,
                outcome=ResolverOutcome.HIT,
                latency_ms=120,
                candidates_returned=8,
            ),
            ResolverRunCreate(
                resolver="serp",
                field="person_name",
                tier=0,
                outcome=ResolverOutcome.MISS,
                latency_ms=300,
            ),
            ResolverRunCreate(
                resolver="smtp",
                field="email",
                tier=0,
                outcome=ResolverOutcome.ERROR,
                latency_ms=5000,
                error="connection refused",
            ),
        ]
    )

    assert written == 3

    rows = (await db_session.execute(select(ResolverRun))).scalars().all()
    outcomes = {row.outcome for row in rows}
    assert outcomes == {ResolverOutcome.HIT, ResolverOutcome.MISS, ResolverOutcome.ERROR}
    assert all(row.ran_at is not None for row in rows)


async def test_record_many_of_empty_list_writes_nothing(db_session: AsyncSession) -> None:
    assert await ResolverRunRepository(db_session).record_many([]) == 0


# ---------------------------------------------------------------------------
# SeedTest
# ---------------------------------------------------------------------------


async def test_latest_placement_per_provider_wins(db_session: AsyncSession) -> None:
    """Reputation changes; averaging a good week with a bad day describes
    neither.
    """
    campaign = await CampaignRepository(db_session).create(CampaignCreate(name="c"))
    repo = SeedTestRepository(db_session)
    now = datetime.now(UTC)

    await repo.record(
        SeedTestCreate(
            campaign_id=campaign.id,
            provider="gmail",
            mailbox_address="seed@gmail.com",
            placement=SeedPlacement.INBOX,
            tested_at=now - timedelta(days=2),
        )
    )
    await repo.record(
        SeedTestCreate(
            campaign_id=campaign.id,
            provider="gmail",
            mailbox_address="seed@gmail.com",
            placement=SeedPlacement.SPAM,
            tested_at=now,
        )
    )
    await repo.record(
        SeedTestCreate(
            campaign_id=campaign.id,
            provider="outlook",
            mailbox_address="seed@outlook.com",
            placement=SeedPlacement.INBOX,
            tested_at=now,
        )
    )

    latest = await repo.latest_per_provider(campaign.id)

    assert latest == {"gmail": SeedPlacement.SPAM, "outlook": SeedPlacement.INBOX}
    assert await repo.spam_rate(campaign.id) == 0.5


async def test_spam_rate_with_no_readings_is_zero(db_session: AsyncSession) -> None:
    """'Never tested' is not 'perfect placement'. The number is 0.0 and the
    UI is responsible for saying which of the two it means.
    """
    campaign = await CampaignRepository(db_session).create(CampaignCreate(name="c"))
    assert await SeedTestRepository(db_session).spam_rate(campaign.id) == 0.0
