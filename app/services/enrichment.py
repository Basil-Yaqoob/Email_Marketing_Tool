"""Enrichment — turn a company row into a named contact with an address.

This is the stage the product's headline number comes from: the share of
discovered companies that end up with a real decision-maker email. It runs
the waterfall per company per field, records every attempt as telemetry,
and writes each accepted value as a Fact carrying its source URL and
confidence before it is ever promoted to a person or email row.

Provenance is not optional here (CLAUDE.md §2.2). Every value written
lands in `facts` first, with the resolver that found it and the page it
came from. The `people` and `email_addresses` rows are a denormalised
convenience for querying; the fact rows are the record of *why* the system
believes them, and are what the UI shows when a lead is expanded.

The batch guard is what stops this stage from becoming the prototype: if
crawling starts failing across the board -- a bad deploy, a dead DNS
resolver, an IP that just got blocked -- the run aborts loudly partway
rather than writing a handful of results and reporting success.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import UpstreamError
from app.db.models.company import Company
from app.db.models.enums import CampaignStatus, ResolverOutcome, RoleClass, SubjectType
from app.db.repositories.campaign_repository import CampaignRepository
from app.db.repositories.company_repository import CompanyRepository
from app.db.repositories.email_repository import (
    EmailAddressCreate,
    EmailAddressRepository,
)
from app.db.repositories.fact_repository import FactCreate, FactRepository
from app.db.repositories.person_repository import PersonCreate, PersonRepository
from app.db.repositories.resolver_run_repository import (
    ResolverRunCreate,
    ResolverRunRepository,
)
from app.resolvers.base import Candidate, LeadContext
from app.resolvers.company.website import WebsiteEmailResolver, WebsitePersonResolver
from app.resolvers.executor import Resolution, ResolverAttempt, run_waterfall
from app.resolvers.registry import ResolverRegistry, build_registry
from app.services.factories import Runtime
from app.services.pipeline import StageResult, guard_error_rate

log = structlog.get_logger(__name__)

# Fields this stage resolves, in order. person_name first: an email
# resolved while a person is already known can be attributed to them,
# and the reverse ordering would leave every address unattached.
_FIELDS = ("person_name", "email")


@dataclass(slots=True)
class _CompanyOutcome:
    """What enrichment produced for one company, before it is written."""

    company_id: uuid.UUID
    person: Candidate | None = None
    email: Candidate | None = None
    attempts: list[ResolverAttempt] = dataclass_field(default_factory=list)
    error: str | None = None

    @property
    def found_anything(self) -> bool:
        return self.person is not None or self.email is not None

    @property
    def error_attempts(self) -> list[ResolverAttempt]:
        return [a for a in self.attempts if a.outcome is ResolverOutcome.ERROR]

    @property
    def failed_outright(self) -> bool:
        """True when nothing was found *and* nothing was actually fetched.

        The distinction the batch guard depends on. `run_waterfall` never
        raises -- it converts a resolver exception into an ERROR attempt on
        purpose, so one dead site cannot kill a whole run, and leaves the
        batch-level abort to decide whether the *rate* of failures means
        something systemic broke.

        Reading only exceptions that escaped therefore counts zero errors
        forever: a run where every site failed DNS looks identical to one
        where every site was crawled cleanly and simply had no contact
        page. That is the prototype's exact failure, and it has to be read
        off the attempts.
        """
        if self.found_anything:
            return False
        if self.error is not None:
            return True
        return bool(self.attempts) and all(
            a.outcome is ResolverOutcome.ERROR for a in self.attempts
        )

    @property
    def failure_reason(self) -> str:
        if self.error is not None:
            return self.error
        errors = self.error_attempts
        if errors:
            return f"{errors[0].resolver}: {errors[0].error}"
        return "no resolver produced a result or an error"


def build_enrichment_registry(runtime: Runtime) -> ResolverRegistry:
    """The free website-crawling tier.

    Both resolvers share one HttpClient, so whichever runs second sees the
    on-disk cache rather than re-fetching pages the first already pulled
    (Session 04's cache only pays for itself when it is shared).
    """
    return build_registry(
        [
            WebsitePersonResolver(http=runtime.http),
            WebsiteEmailResolver(http=runtime.http),
        ]
    )


def _context_for(company: Company) -> LeadContext:
    return LeadContext(
        company_id=company.id,
        company_name=company.name,
        domain=company.domain,
        website=company.website,
        country_code=company.country_code,
    )


async def _resolve_company(
    company: Company, registry: ResolverRegistry, *, threshold: float
) -> _CompanyOutcome:
    """Run the waterfall for each field against one company.

    A crawl failure for one company is routine -- sites go down, DNS
    fails, a host blocks us -- and is recorded against that company rather
    than aborting the batch. The batch guard decides whether the *rate* of
    those failures means something systemic is wrong.
    """
    outcome = _CompanyOutcome(company_id=company.id)
    ctx = _context_for(company)

    for field_name in _FIELDS:
        try:
            resolution: Resolution = await run_waterfall(
                field=field_name, ctx=ctx, registry=registry, threshold=threshold
            )
        except UpstreamError as exc:
            outcome.error = f"{field_name}: {exc}"
            continue

        outcome.attempts.extend(resolution.attempts)
        if resolution.best is None:
            continue

        if field_name == "person_name":
            outcome.person = resolution.best
            # Feed the person forward so the email resolver can attribute
            # an address to a known name rather than to the company at
            # large.
            ctx = LeadContext(
                company_id=ctx.company_id,
                company_name=ctx.company_name,
                domain=ctx.domain,
                website=ctx.website,
                country_code=ctx.country_code,
                person_name=resolution.best.value,
            )
        else:
            outcome.email = resolution.best

    return outcome


async def _persist(session: AsyncSession, outcome: _CompanyOutcome) -> None:
    """Write facts first, then the denormalised rows.

    Order matters: the fact is the evidence, the person/email row is the
    convenience view of it. Writing the row without the fact would leave a
    value in the database that nothing can explain -- exactly what rule
    2.2 forbids.
    """
    facts = FactRepository(session)
    now = datetime.now(UTC)
    person_id: uuid.UUID | None = None

    if outcome.person is not None:
        await facts.record(
            FactCreate(
                subject_type=SubjectType.COMPANY,
                subject_id=outcome.company_id,
                field="person_name",
                value=outcome.person.value,
                confidence=outcome.person.confidence,
                source=outcome.person.source,
                source_url=outcome.person.source_url,
                retrieved_at=now,
            )
        )
        title = outcome.person.extra.get("title") if outcome.person.extra else None
        person = await PersonRepository(session).add(
            PersonCreate(
                company_id=outcome.company_id,
                full_name=outcome.person.value,
                title=str(title) if title else None,
                role_class=_role_class_of(outcome.person),
            )
        )
        person_id = person.id

    if outcome.email is not None:
        await facts.record(
            FactCreate(
                subject_type=SubjectType.COMPANY,
                subject_id=outcome.company_id,
                field="email",
                value=outcome.email.value,
                confidence=outcome.email.confidence,
                source=outcome.email.source,
                source_url=outcome.email.source_url,
                retrieved_at=now,
            )
        )
        extra = outcome.email.extra or {}
        await EmailAddressRepository(session).add(
            EmailAddressCreate(
                company_id=outcome.company_id,
                person_id=person_id,
                address=outcome.email.value,
                pattern_used=str(extra.get("strategy")) if extra.get("strategy") else None,
                is_role_account=bool(extra.get("is_role_account", False)),
            )
        )


def _role_class_of(candidate: Candidate) -> RoleClass:
    raw = (candidate.extra or {}).get("role_class")
    if isinstance(raw, RoleClass):
        return raw
    if isinstance(raw, str):
        try:
            return RoleClass(raw)
        except ValueError:
            return RoleClass.OTHER
    return RoleClass.OTHER


async def _record_attempts(session: AsyncSession, outcomes: list[_CompanyOutcome]) -> None:
    """One telemetry row per resolver attempt, hit or miss.

    This is what `app/analytics/yield_report.py` reads to notice a
    resolver whose hit rate collapsed. A stage that only logged successes
    would make a total crawl failure look identical to a run against
    companies that happened to have no contact page.
    """
    runs = [
        ResolverRunCreate(
            resolver=attempt.resolver,
            field=attempt.field,
            subject_id=outcome.company_id,
            tier=int(attempt.tier),
            outcome=attempt.outcome,
            candidates_returned=attempt.candidates_returned,
            latency_ms=attempt.latency_ms,
            cost=attempt.cost,
            error=attempt.error,
        )
        for outcome in outcomes
        for attempt in outcome.attempts
    ]
    await ResolverRunRepository(session).record_many(runs)


async def run_enrichment(
    session: AsyncSession,
    runtime: Runtime,
    campaign_id: uuid.UUID,
    *,
    limit: int = 200,
    registry: ResolverRegistry | None = None,
) -> StageResult:
    """Crawl each company's website for a contact, and persist what is found.

    Only companies that have a website are attempted. A company without
    one is not an enrichment failure -- there is nothing to fetch -- so it
    is excluded from the denominator rather than counted as a miss, which
    would make the hit rate meaningless.
    """
    campaigns = CampaignRepository(session)
    if await campaigns.get(campaign_id) is None:
        raise ValueError(f"Campaign {campaign_id} does not exist")

    companies = await CompanyRepository(session).with_website(campaign_id, limit=limit)
    result = StageResult(stage="enrichment", attempted=len(companies))
    if not companies:
        log.info("enrichment.nothing_to_do", campaign_id=str(campaign_id))
        return result

    resolvers = registry or build_enrichment_registry(runtime)
    threshold = runtime.settings.confidence_threshold

    await campaigns.set_status(campaign_id, CampaignStatus.ENRICHING)
    outcomes: list[_CompanyOutcome] = []

    for company in companies:
        outcome = await _resolve_company(company, resolvers, threshold=threshold)
        outcomes.append(outcome)

        if outcome.found_anything:
            result.found += 1
            await _persist(session, outcome)
            result.written += 1
        elif outcome.failed_outright:
            # Never fetched, as opposed to fetched-and-had-no-contact-page.
            # Conflating the two is what makes a total crawl failure look
            # like an honest 0% yield.
            result.errors.append(outcome.failure_reason)
        else:
            result.skipped += 1

    await _record_attempts(session, outcomes)
    await campaigns.set_status(campaign_id, CampaignStatus.DRAFT)

    log.info(
        "enrichment.complete",
        campaign_id=str(campaign_id),
        attempted=result.attempted,
        found=result.found,
        no_contact_found=result.skipped,
        failed_to_fetch=len(result.errors),
        hit_rate=round(result.hit_rate, 3),
        error_rate=round(result.error_rate, 3),
    )

    # Checked last so telemetry and partial results are durable before the
    # abort propagates -- a guard that fires must leave behind the evidence
    # of why, not discard it.
    guard_error_rate(result, threshold=runtime.settings.error_rate_threshold)
    return result


__all__ = ["build_enrichment_registry", "run_enrichment"]
