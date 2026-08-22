"""Email resolution and verification — the two stages that decide whether
a lead is contactable.

They are separate on purpose, because their economics are opposite.
*Generating* candidate addresses is free: it is string manipulation over a
name and a domain. *Verifying* them costs real time and real reputation
(every SMTP probe is a connection to someone's mail server). So generation
runs wide and verification runs narrow, and the domain-pattern store is
what connects them -- one confirmed address turns nine future guesses at
that domain into one.

Verification's honest ceiling is stated in CLAUDE.md §10 and is enforced
here rather than papered over: Google Workspace and Microsoft 365 accept
mail for every address at a domain, so a probe cannot distinguish a real
mailbox from a typo. Those come back UNKNOWN. UNKNOWN is a *conclusion*,
not a failure, and the funnel and UI must say so -- a lead marked UNKNOWN
is sent on the lower-volume track, not discarded and not counted as an
error.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.enums import CampaignStatus, ResolverOutcome, SubjectType, VerifyStatus
from app.db.repositories.campaign_repository import CampaignRepository
from app.db.repositories.company_repository import CompanyRepository
from app.db.repositories.domain_pattern_repository import DomainPatternRepository
from app.db.repositories.email_repository import (
    EmailAddressCreate,
    EmailAddressRepository,
)
from app.db.repositories.fact_repository import FactCreate, FactRepository
from app.db.repositories.person_repository import PersonRepository
from app.db.repositories.resolver_run_repository import (
    ResolverRunCreate,
    ResolverRunRepository,
)
from app.resolvers.base import LeadContext
from app.resolvers.email.patterns import KnownPattern
from app.resolvers.email.resolver import PatternEmailResolver
from app.resolvers.email.verify.mx import DnsPythonResolver, DnsResolver
from app.resolvers.email.verify.orchestrator import verify_batch
from app.resolvers.email.verify.smtp_probe import SMTPProber
from app.services.factories import Runtime
from app.services.pipeline import StageResult, guard_error_rate

log = structlog.get_logger(__name__)


class RepositoryPatternStore:
    """Adapts DomainPatternRepository to the resolver's PatternStore.

    The repository returns a SQLAlchemy row; the resolver wants a
    KnownPattern. Converting here keeps persistence models from crossing
    into the resolver layer (CLAUDE.md §6) -- the resolver stays testable
    with a fake store and no database.
    """

    def __init__(self, repository: DomainPatternRepository) -> None:
        self._repository = repository

    async def get(self, domain: str) -> KnownPattern | None:
        row = await self._repository.get(domain)
        if row is None:
            return None
        return KnownPattern(pattern=row.pattern, confirmed_count=row.confirmed_count)


async def run_email_resolution(
    session: AsyncSession,
    runtime: Runtime,
    campaign_id: uuid.UUID,
    *,
    limit: int = 500,
) -> StageResult:
    """Generate candidate addresses for companies that have a person and a
    domain but no address yet.

    Companies already holding an address are skipped rather than
    re-guessed: the crawler found a real one, and a generated guess must
    never displace an address that was actually published.
    """
    campaigns = CampaignRepository(session)
    if await campaigns.get(campaign_id) is None:
        raise ValueError(f"Campaign {campaign_id} does not exist")

    companies = await CompanyRepository(session).list_for_campaign(campaign_id, limit=limit)
    people = PersonRepository(session)
    emails = EmailAddressRepository(session)
    facts = FactRepository(session)
    resolver = PatternEmailResolver(store=RepositoryPatternStore(DomainPatternRepository(session)))

    result = StageResult(stage="email_resolution")
    attempts: list[ResolverRunCreate] = []
    now = datetime.now(UTC)

    for company in companies:
        if not company.domain:
            continue
        if await emails.for_company(company.id):
            continue  # a real address already beats any guess
        person = await people.best_for_company(company.id)
        if person is None:
            continue

        result.attempted += 1
        ctx = LeadContext(
            company_id=company.id,
            company_name=company.name,
            domain=company.domain,
            website=company.website,
            country_code=company.country_code,
            person_name=person.full_name,
        )

        candidates = await resolver.resolve(ctx)
        attempts.append(
            ResolverRunCreate(
                resolver=resolver.name,
                field="email",
                subject_id=company.id,
                tier=int(resolver.tier),
                outcome=ResolverOutcome.HIT if candidates else ResolverOutcome.MISS,
                candidates_returned=len(candidates),
                latency_ms=0,
            )
        )
        if not candidates:
            result.skipped += 1
            continue

        result.found += 1
        for candidate in candidates:
            await facts.record(
                FactCreate(
                    subject_type=SubjectType.COMPANY,
                    subject_id=company.id,
                    field="email",
                    value=candidate.value,
                    confidence=candidate.confidence,
                    source=candidate.source,
                    source_url=candidate.source_url,
                    retrieved_at=now,
                )
            )
            await emails.add(
                EmailAddressCreate(
                    company_id=company.id,
                    person_id=person.id,
                    address=candidate.value,
                    pattern_used=str((candidate.extra or {}).get("pattern") or "") or None,
                )
            )
            result.written += 1

    await ResolverRunRepository(session).record_many(attempts)
    log.info(
        "email_resolution.complete",
        campaign_id=str(campaign_id),
        attempted=result.attempted,
        with_candidates=result.found,
        addresses_written=result.written,
    )
    return result


async def run_verification(
    session: AsyncSession,
    runtime: Runtime,
    campaign_id: uuid.UUID,
    *,
    limit: int = 200,
    dns: DnsResolver | None = None,
    prober: SMTPProber | None = None,
) -> StageResult:
    """Walk the verification ladder over every unchecked address.

    `prober=None` means rung 3 (the SMTP probe) is not deployed, which is
    the normal case on a residential connection: port 25 is almost always
    blocked outbound. Everything then settles at UNKNOWN rather than
    crashing or guessing -- honest, and exactly what §10 promises.

    A VALID result also teaches the domain-pattern store, which is what
    makes the next campaign at the same domain cost one guess instead of
    nine.
    """
    campaigns = CampaignRepository(session)
    if await campaigns.get(campaign_id) is None:
        raise ValueError(f"Campaign {campaign_id} does not exist")

    emails = EmailAddressRepository(session)
    pending = await emails.pending_verification(campaign_id, limit=limit)

    result = StageResult(stage="verification", attempted=len(pending))
    if not pending:
        log.info("verification.nothing_to_do", campaign_id=str(campaign_id))
        return result

    people = PersonRepository(session)
    names: dict[str, str] = {}
    for row in pending:
        if row.person_id is not None:
            person = await people.get(row.person_id)
            if person is not None:
                names[row.address] = person.full_name

    await campaigns.set_status(campaign_id, CampaignStatus.ENRICHING)
    batch = await verify_batch(
        [row.address for row in pending],
        resolver=dns or DnsPythonResolver(),
        prober=prober,
        names=names,
        error_threshold=runtime.settings.error_rate_threshold,
    )

    by_address = {r.address: r for r in batch.results}
    for row in pending:
        verification = by_address.get(row.address)
        if verification is None:
            continue
        await emails.set_verify_status(row.id, verification.status)
        if verification.status == VerifyStatus.VALID:
            result.found += 1
        result.written += 1

    # Feed confirmed addresses back into the global pattern store.
    patterns = DomainPatternRepository(session)
    for learned in batch.learned:
        await patterns.learn(learned.domain, learned.pattern, weight=learned.weight)

    await ResolverRunRepository(session).record_many(
        [
            ResolverRunCreate(
                resolver="verify_ladder",
                field="email",
                subject_id=None,
                tier=0,
                outcome=ResolverOutcome.HIT if result.found else ResolverOutcome.MISS,
                candidates_returned=result.found,
                latency_ms=0,
            )
        ]
    )

    result.errors.extend(["verification error"] * batch.error_count)
    await campaigns.set_status(campaign_id, CampaignStatus.DRAFT)

    log.info(
        "verification.complete",
        campaign_id=str(campaign_id),
        checked=batch.checked,
        valid=batch.count(VerifyStatus.VALID),
        invalid=batch.count(VerifyStatus.INVALID),
        # Reported separately from errors on purpose: UNKNOWN is a
        # conclusion the ladder reached, not a failure to reach one.
        unknown=batch.count(VerifyStatus.UNKNOWN),
        catch_all=batch.count(VerifyStatus.CATCH_ALL),
        # Reported too, or a batch of nothing but role accounts logs
        # 'checked=1' with every count at zero and reads as if the ladder
        # did nothing.
        role=batch.count(VerifyStatus.ROLE),
        errors=batch.error_count,
        patterns_learned=len(batch.learned),
    )

    guard_error_rate(result, threshold=runtime.settings.error_rate_threshold)
    return result


__all__ = ["RepositoryPatternStore", "run_email_resolution", "run_verification"]
