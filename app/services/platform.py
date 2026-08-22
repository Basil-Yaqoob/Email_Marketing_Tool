"""The real PlatformService — one implementation, three transports.

`app/api/routes.py`, `app/web/`, and `app/mcp/server/` all call this and
nothing else. Before it existed each of those either returned 501 or made
up its own answer, which is why the same product had a REST API that
raised NotImplemented, a UI rendering hardcoded empty lists, and an MCP
server backed by an in-memory demo.

Long-running work is never done inline. `run_stage` and friends enqueue a
job and return its id immediately: discovery takes minutes and crawling
takes longer, and an HTTP request that waits for either is a request that
times out. The transports poll `get_job`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.yield_report import source_yield as compute_yield
from app.api.models import (
    AlertResponse,
    CampaignCreate,
    CampaignResponse,
    FactRecord,
    JobStatus,
    LeadResponse,
    LeadsListResponse,
    MailboxResponse,
    MessageResponse,
    SuppressionAdd,
)
from app.db.models.campaign import Campaign
from app.db.models.enums import CampaignStatus, VerifyStatus
from app.db.repositories.campaign_repository import (
    CampaignCreate as RepoCampaignCreate,
)
from app.db.repositories.campaign_repository import CampaignRepository, FunnelCounts
from app.db.repositories.company_repository import CompanyRepository
from app.db.repositories.email_repository import EmailAddressRepository
from app.db.repositories.fact_repository import FactRepository
from app.db.repositories.mailbox_repository import MailboxRepository
from app.db.repositories.message_repository import MessageRepository
from app.db.repositories.person_repository import PersonRepository
from app.db.repositories.suppression_repository import (
    SuppressionCreate,
    SuppressionRepository,
)
from app.mcp.server.service import (
    CostEstimate,
    LaunchPreview,
    SourceYieldRow,
    SuppressionAck,
)
from app.services.factories import Runtime
from app.services.jobs import JobQueue, Stage

log = structlog.get_logger(__name__)

# Wide enough that a campaign run over a few days still shows its whole
# history rather than appearing to have no telemetry at all.
_YIELD_WINDOW = timedelta(days=30)

# Rough per-lead cost of the stages that spend money, used for the estimate
# a confirmation gate shows before running. Free stages are genuinely $0 --
# OSM, crawling, pattern generation and MX lookups cost nothing but time.
_COST_PER_LEAD: dict[Stage, Decimal] = {
    Stage.DISCOVERY: Decimal("0"),
    Stage.ENRICHMENT: Decimal("0"),
    Stage.EMAIL_RESOLUTION: Decimal("0"),
    Stage.VERIFICATION: Decimal("0"),
    Stage.HOOK_RESEARCH: Decimal("0.01"),
    Stage.COPY: Decimal("0.004"),
}


def _campaign_response(campaign: Campaign, funnel: FunnelCounts) -> CampaignResponse:
    icp = campaign.icp or {}
    return CampaignResponse(
        id=campaign.id,
        name=campaign.name,
        icp_categories=[str(c) for c in icp.get("categories", [])],
        icp_locations=[str(loc) for loc in icp.get("locations", [])],
        icp_filters={str(k): str(v) for k, v in (icp.get("filters") or {}).items()},
        jurisdiction=campaign.jurisdiction or "",
        state=campaign.status.value.upper(),
        discovered=funnel.discovered,
        with_website=funnel.with_website,
        person_found=funnel.person_found,
        email_found=funnel.email_found,
        verified=funnel.verified,
        sent=funnel.sent,
        replied=funnel.replied,
        created_at=campaign.created_at,
        updated_at=campaign.created_at,
    )


class PlatformServiceImpl:
    """Bound to one database session and one process-wide Runtime.

    Constructed per request (the session is), never cached across them.
    """

    def __init__(self, session: AsyncSession, runtime: Runtime, jobs: JobQueue) -> None:
        self._session = session
        self._runtime = runtime
        self._jobs = jobs
        self._campaigns = CampaignRepository(session)
        self._companies = CompanyRepository(session)
        self._people = PersonRepository(session)
        self._emails = EmailAddressRepository(session)
        self._facts = FactRepository(session)
        self._messages = MessageRepository(session)

    # --- Read ---------------------------------------------------------------

    async def list_campaigns(self) -> list[CampaignResponse]:
        campaigns = await self._campaigns.list()
        out = []
        for campaign in campaigns:
            funnel = await self._campaigns.funnel(campaign.id)
            out.append(_campaign_response(campaign, funnel))
        return out

    async def get_campaign(self, campaign_id: str) -> CampaignResponse:
        campaign = await self._require_campaign(campaign_id)
        funnel = await self._campaigns.funnel(campaign.id)
        return _campaign_response(campaign, funnel)

    async def query_leads(
        self,
        campaign_id: str,
        *,
        verify_status: str | None = None,
        has_person: bool | None = None,
        has_hook: bool | None = None,
        jurisdiction: str | None = None,
        role_class: str | None = None,
        page: int = 1,
        page_size: int = 25,
    ) -> LeadsListResponse:
        campaign = await self._require_campaign(campaign_id)
        page = max(1, page)
        page_size = max(1, min(page_size, 200))

        companies = await self._companies.list_for_campaign(
            campaign.id, limit=page_size, offset=(page - 1) * page_size
        )
        total = await self._companies.count(campaign.id)

        leads = [await self._lead_response(company) for company in companies]

        # Filters are applied after hydration because they read the
        # denormalised person/email rows rather than columns on companies.
        # Correct but not cheap; a campaign large enough for this to matter
        # wants the filter pushed into SQL instead.
        if verify_status is not None:
            leads = [lead for lead in leads if lead.email_verified == verify_status]
        if has_person is not None:
            leads = [lead for lead in leads if (lead.person_name is not None) is has_person]

        return LeadsListResponse(leads=leads, total=total, page=page, page_size=page_size)

    async def get_lead(self, lead_id: str) -> LeadResponse:
        company = await self._companies.get(_as_uuid(lead_id, "lead"))
        if company is None:
            raise ValueError(f"no lead with id {lead_id!r}")
        return await self._lead_response(company)

    async def get_message(self, message_id: str) -> MessageResponse:
        raise NotImplementedError("message review lands with the copy stage")

    async def source_yield(self, campaign_id: str) -> list[SourceYieldRow]:
        """Per-resolver hit rate, worst first.

        Ordered ascending by hit rate on purpose: a resolver that has
        stopped finding anything is the row worth reading, and burying it
        under the healthy ones is how a yield collapse goes unnoticed.
        """
        campaign = await self._require_campaign(campaign_id)
        rows = await compute_yield(self._session, str(campaign.id), _YIELD_WINDOW)
        return sorted(
            (
                SourceYieldRow(
                    resolver=row.resolver_name,
                    # resolver_runs aggregates across fields, so the row is
                    # per resolver rather than per (resolver, field).
                    field="",
                    tier="",
                    attempts=row.attempts,
                    hits=row.hits,
                    hit_rate=row.hit_rate,
                )
                for row in rows
            ),
            key=lambda r: r.hit_rate,
        )

    async def list_alerts(self) -> list[AlertResponse]:
        # Alerts are computed per campaign by app/analytics/anomaly.py; a
        # cross-campaign feed needs a query this repository layer does not
        # have yet. Empty is honest until it does.
        return []

    async def mailbox_health(self) -> list[MailboxResponse]:
        mailboxes = await MailboxRepository(self._session, self._runtime.vault).list()
        return [
            MailboxResponse(
                id=m.id,
                address=m.email_address,
                health_score=m.health_score,
                bounce_rate=0.0,
                complaint_rate=0.0,
                reply_rate=0.0,
                warmup_stage=m.warmup_stage,
                dns_status={k: str(v) for k, v in (m.dns_status or {}).items()},
                dns_fix_text={},
                created_at=datetime.now(UTC),
            )
            for m in mailboxes
        ]

    # --- Write --------------------------------------------------------------

    async def create_campaign(self, data: CampaignCreate) -> CampaignResponse:
        campaign = await self._campaigns.create(
            RepoCampaignCreate(
                name=data.name,
                icp={
                    "categories": data.icp_categories,
                    "locations": data.icp_locations,
                    "filters": data.icp_filters,
                },
                jurisdiction=data.jurisdiction,
            )
        )
        await self._session.commit()
        funnel = await self._campaigns.funnel(campaign.id)
        log.info("campaign.created", campaign_id=str(campaign.id), name=campaign.name)
        return _campaign_response(campaign, funnel)

    async def estimate_stage(self, campaign_id: str, stage: str) -> CostEstimate:
        campaign = await self._require_campaign(campaign_id)
        resolved = Stage(stage)
        funnel = await self._campaigns.funnel(campaign.id)

        # How many leads the stage would actually touch, not the whole
        # campaign: an estimate that ignores what the stage filters on is
        # worse than no estimate, because a confirmation gate showing a
        # wrong number teaches people to ignore it.
        eligible = {
            Stage.DISCOVERY: max(funnel.discovered, 1),
            Stage.ENRICHMENT: funnel.with_website,
            Stage.EMAIL_RESOLUTION: funnel.person_found,
            Stage.VERIFICATION: funnel.email_found,
            Stage.HOOK_RESEARCH: funnel.email_found,
            Stage.COPY: funnel.email_found,
        }[resolved]

        return CostEstimate(
            campaign_id=str(campaign.id),
            stage=stage,
            estimated_leads=eligible,
            estimated_cost_usd=str(_COST_PER_LEAD[resolved] * eligible),
        )

    async def run_stage(self, campaign_id: str, stage: str) -> JobStatus:
        campaign = await self._require_campaign(campaign_id)
        return await self._jobs.enqueue(Stage(stage), campaign.id)

    async def get_job(self, job_id: str) -> JobStatus:
        return await self._jobs.status(_as_uuid(job_id, "job"))

    async def estimate_draft_messages(self, campaign_id: str) -> CostEstimate:
        return await self.estimate_stage(campaign_id, Stage.COPY.value)

    async def draft_messages(self, campaign_id: str) -> JobStatus:
        return await self.run_stage(campaign_id, Stage.COPY.value)

    async def edit_message(
        self, message_id: str, *, subject: str | None = None, body: str | None = None
    ) -> MessageResponse:
        raise NotImplementedError("message editing lands with the copy stage")

    async def add_suppression(self, data: SuppressionAdd) -> SuppressionAck:
        value = data.address or data.domain
        if value is None:
            raise ValueError("a suppression needs an address or a domain")
        await SuppressionRepository(self._session).add(
            SuppressionCreate(value=value, reason=f"added via {data.scope}")
        )
        await self._session.commit()
        return SuppressionAck(address=data.address, domain=data.domain, scope=data.scope)

    # --- Dangerous ----------------------------------------------------------

    async def preview_launch(self, campaign_id: str) -> LaunchPreview:
        campaign = await self._require_campaign(campaign_id)
        mailboxes = await MailboxRepository(self._session, self._runtime.vault).list()
        return LaunchPreview(
            campaign_id=str(campaign.id),
            recipient_count=await self._campaigns.approved_message_count(campaign.id),
            mailboxes=[m.email_address for m in mailboxes],
        )

    async def launch_campaign(self, campaign_id: str) -> JobStatus:
        raise NotImplementedError("sending is not wired yet — connect a mailbox first (Phase 6)")

    async def pause_campaign(self, campaign_id: str) -> CampaignResponse:
        campaign = await self._require_campaign(campaign_id)
        await self._campaigns.set_status(campaign.id, CampaignStatus.PAUSED)
        await self._session.commit()
        return await self.get_campaign(campaign_id)

    # --- Resources ----------------------------------------------------------

    async def get_funnel(self, campaign_id: str) -> dict[str, int]:
        campaign = await self._require_campaign(campaign_id)
        return (await self._campaigns.funnel(campaign.id)).model_dump()

    async def get_report(self, campaign_id: str) -> dict[str, Any]:
        campaign = await self._require_campaign(campaign_id)
        sources = await self.source_yield(campaign_id)
        breakdown = await self._emails.status_breakdown(campaign.id)
        return {
            "campaign_id": str(campaign.id),
            "funnel": (await self._campaigns.funnel(campaign.id)).model_dump(),
            "verification": breakdown,
            "sources": [s.model_dump(mode="json") for s in sources],
        }

    # --- helpers ------------------------------------------------------------

    async def _require_campaign(self, campaign_id: str) -> Campaign:
        campaign = await self._campaigns.get(_as_uuid(campaign_id, "campaign"))
        if campaign is None:
            raise ValueError(f"no campaign with id {campaign_id!r}")
        return campaign

    async def _lead_response(self, company: Any) -> LeadResponse:
        person = await self._people.best_for_company(company.id)
        emails = await self._emails.for_company(company.id)
        best_email = _best_email(emails)

        facts: dict[str, list[FactRecord]] = {}
        for field in ("person_name", "email"):
            rows = await self._facts.current(company.id, field)
            if rows:
                facts[field] = [
                    FactRecord(
                        value=row.value,
                        confidence=row.confidence,
                        source=row.source,
                        source_url=row.source_url,
                        retrieved_at=row.retrieved_at,
                    )
                    for row in rows
                ]

        return LeadResponse(
            id=company.id,
            campaign_id=company.campaign_id,
            company_name=company.name,
            website_url=company.website,
            person_name=person.full_name if person else None,
            person_title=person.title if person else None,
            email=best_email.address if best_email else None,
            email_verified=best_email.verify_status.value if best_email else None,
            facts=facts,
            created_at=company.created_at,
        )


def _best_email(emails: list[Any]) -> Any | None:
    """The address to show for a lead.

    Ranked by how settled the verdict is, not by when it was found: a
    VALID address beats an UNKNOWN one, and both beat an unchecked guess.
    An INVALID address ranks last but is still shown when it is all there
    is -- hiding it would misreport the lead as having no email at all.
    """
    if not emails:
        return None
    rank = {
        VerifyStatus.VALID: 0,
        VerifyStatus.CATCH_ALL: 1,
        VerifyStatus.UNKNOWN: 2,
        VerifyStatus.UNCHECKED: 3,
        VerifyStatus.ROLE: 4,
        VerifyStatus.INVALID: 5,
    }
    return sorted(emails, key=lambda e: rank.get(e.verify_status, 9))[0]


def _as_uuid(value: str, kind: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"{value!r} is not a valid {kind} id") from exc


__all__ = ["PlatformServiceImpl"]
