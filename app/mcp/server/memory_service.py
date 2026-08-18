"""A reference PlatformService, in memory, seeded with one demo campaign.

This is what `python -m app.mcp.server` runs against today. It is not the
production data layer -- see app.mcp.server.service's module docstring for
why that doesn't exist yet (no CampaignRepository/LeadRepository has been
built; Session 20's routes.py is in the same position). This exists so the
README's config block is actually runnable and a real MCP client (Claude
Desktop, Claude Code, or the SDK's own ClientSession) can connect to a live
process and see real tool/resource behaviour today, rather than the server
only existing as something tests drive in-process.

Whichever orchestration session builds the real repositories gets a drop-in
replacement: implement PlatformService against them and pass that instance
to app.mcp.server.app.build_server instead of this one. No tool or resource
code changes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TypeVar
from uuid import uuid4

from app.api.models import (
    AlertResponse,
    CampaignCreate,
    CampaignResponse,
    FactRecord,
    JobState,
    JobStatus,
    LeadResponse,
    LeadsListResponse,
    MailboxResponse,
    MessageResponse,
    SuppressionAdd,
)
from app.mcp.server.service import (
    CostEstimate,
    LaunchPreview,
    SourceYieldRow,
    SuppressionAck,
)

_DEMO_CAMPAIGN_ID = str(uuid4())
_DEMO_LEAD_ID = str(uuid4())
_DEMO_MESSAGE_ID = str(uuid4())

T = TypeVar("T")


class InMemoryPlatformService:
    """Seeded with one demo campaign so a freshly-connected client has
    something to look at. All state is process-local and lost on restart.
    """

    def __init__(self) -> None:
        now = datetime.now(UTC)
        self._campaigns: dict[str, CampaignResponse] = {
            _DEMO_CAMPAIGN_ID: CampaignResponse(
                id=_DEMO_CAMPAIGN_ID,
                name="Demo: Austin Dental Clinics",
                icp_categories=["dentist"],
                icp_locations=["Austin, TX"],
                icp_filters={},
                jurisdiction="US",
                state="DRAFT",
                discovered=42,
                with_website=31,
                person_found=24,
                email_found=19,
                verified=12,
                sent=0,
                replied=0,
                created_at=now,
                updated_at=now,
            )
        }
        self._leads: dict[str, LeadResponse] = {
            _DEMO_LEAD_ID: LeadResponse(
                id=_DEMO_LEAD_ID,
                campaign_id=_DEMO_CAMPAIGN_ID,
                company_name="Riverside Dental",
                website_url="https://riversidedental.example",
                person_name="Dr. Jane Smith",
                person_title="Owner",
                email="jane@riversidedental.example",
                email_verified="unknown",
                facts={
                    "person_name": [
                        FactRecord(
                            value="Dr. Jane Smith",
                            confidence=0.82,
                            source="website_crawler",
                            source_url="https://riversidedental.example/about",
                            retrieved_at=now,
                        )
                    ]
                },
                created_at=now,
            )
        }
        self._messages: dict[str, MessageResponse] = {
            _DEMO_MESSAGE_ID: MessageResponse(
                id=_DEMO_MESSAGE_ID,
                campaign_id=_DEMO_CAMPAIGN_ID,
                lead_id=_DEMO_LEAD_ID,
                recipient_email="jane@riversidedental.example",
                subject="quick question about riverside dental",
                body="Hi Jane, ...",
                quality_report={"mechanical_scan": "clean", "critic_score": "8"},
                state="DRAFT",
                created_at=now,
            )
        }
        self._mailboxes: list[MailboxResponse] = [
            MailboxResponse(
                id=str(uuid4()),
                address="outreach@example-sender.com",
                health_score=0.91,
                bounce_rate=0.01,
                complaint_rate=0.0,
                reply_rate=0.04,
                warmup_stage=6,
                dns_status={"spf": "PASS", "dkim": "PASS", "dmarc": "PASS"},
                dns_fix_text={},
                created_at=now,
            )
        ]
        self._alerts: list[AlertResponse] = []
        self._suppressed: set[str] = set()

    # --- Read -----------------------------------------------------------------

    async def list_campaigns(self) -> list[CampaignResponse]:
        return list(self._campaigns.values())

    async def get_campaign(self, campaign_id: str) -> CampaignResponse:
        return self._require(self._campaigns, campaign_id, "campaign")

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
        self._require(self._campaigns, campaign_id, "campaign")
        leads = [lead for lead in self._leads.values() if str(lead.campaign_id) == campaign_id]
        if verify_status is not None:
            leads = [lead for lead in leads if lead.email_verified == verify_status]
        if has_person is not None:
            leads = [lead for lead in leads if (lead.person_name is not None) == has_person]
        start = (page - 1) * page_size
        page_items = leads[start : start + page_size]
        return LeadsListResponse(leads=page_items, total=len(leads), page=page, page_size=page_size)

    async def get_lead(self, lead_id: str) -> LeadResponse:
        return self._require(self._leads, lead_id, "lead")

    async def get_message(self, message_id: str) -> MessageResponse:
        return self._require(self._messages, message_id, "message")

    async def source_yield(self, campaign_id: str) -> list[SourceYieldRow]:
        self._require(self._campaigns, campaign_id, "campaign")
        return [
            SourceYieldRow(
                resolver="osm", field="company", tier="FREE", attempts=42, hits=42, hit_rate=1.0
            ),
            SourceYieldRow(
                resolver="website_crawler",
                field="person_name",
                tier="FREE",
                attempts=31,
                hits=24,
                hit_rate=round(24 / 31, 4),
            ),
        ]

    async def list_alerts(self) -> list[AlertResponse]:
        return list(self._alerts)

    async def mailbox_health(self) -> list[MailboxResponse]:
        return list(self._mailboxes)

    # --- Write ------------------------------------------------------------------

    async def create_campaign(self, data: CampaignCreate) -> CampaignResponse:
        now = datetime.now(UTC)
        campaign_id = str(uuid4())
        campaign = CampaignResponse(
            id=campaign_id,
            name=data.name,
            icp_categories=data.icp_categories,
            icp_locations=data.icp_locations,
            icp_filters=data.icp_filters,
            jurisdiction=data.jurisdiction,
            state="DRAFT",
            discovered=0,
            with_website=0,
            person_found=0,
            email_found=0,
            verified=0,
            sent=0,
            replied=0,
            created_at=now,
            updated_at=now,
        )
        self._campaigns[campaign_id] = campaign
        return campaign

    async def estimate_stage(self, campaign_id: str, stage: str) -> CostEstimate:
        campaign = self._require(self._campaigns, campaign_id, "campaign")
        # Free stages cost nothing; only metered ones project a spend.
        cost_per_lead = Decimal("0.02") if stage == "enrichment" else Decimal("0")
        n_leads = max(campaign.discovered, 1)
        return CostEstimate(
            campaign_id=campaign_id,
            stage=stage,
            estimated_leads=n_leads,
            estimated_cost_usd=str(cost_per_lead * n_leads),
        )

    async def run_stage(self, campaign_id: str, stage: str) -> JobStatus:
        self._require(self._campaigns, campaign_id, "campaign")
        now = datetime.now(UTC)
        return JobStatus(
            id=uuid4(), state=JobState.QUEUED, progress=0.0, processed=0, total=0, created_at=now
        )

    async def estimate_draft_messages(self, campaign_id: str) -> CostEstimate:
        campaign = self._require(self._campaigns, campaign_id, "campaign")
        n_leads = max(campaign.email_found, 1)
        return CostEstimate(
            campaign_id=campaign_id,
            stage="draft_messages",
            estimated_leads=n_leads,
            estimated_cost_usd=str(Decimal("0.15") * n_leads),
        )

    async def draft_messages(self, campaign_id: str) -> JobStatus:
        self._require(self._campaigns, campaign_id, "campaign")
        now = datetime.now(UTC)
        return JobStatus(
            id=uuid4(), state=JobState.QUEUED, progress=0.0, processed=0, total=0, created_at=now
        )

    async def edit_message(
        self, message_id: str, *, subject: str | None = None, body: str | None = None
    ) -> MessageResponse:
        existing = self._require(self._messages, message_id, "message")
        updated = existing.model_copy(
            update={
                "subject": subject if subject is not None else existing.subject,
                "body": body if body is not None else existing.body,
            }
        )
        self._messages[message_id] = updated
        return updated

    async def add_suppression(self, data: SuppressionAdd) -> SuppressionAck:
        key = data.address or data.domain
        if key is None:
            raise ValueError("add_suppression needs an address or a domain")
        self._suppressed.add(key)
        return SuppressionAck(address=data.address, domain=data.domain, scope=data.scope)

    # --- Dangerous ----------------------------------------------------------------

    async def preview_launch(self, campaign_id: str) -> LaunchPreview:
        self._require(self._campaigns, campaign_id, "campaign")
        return LaunchPreview(
            campaign_id=campaign_id,
            recipient_count=len(
                [lead for lead in self._leads.values() if str(lead.campaign_id) == campaign_id]
            ),
            mailboxes=[mb.address for mb in self._mailboxes],
        )

    async def launch_campaign(self, campaign_id: str) -> JobStatus:
        campaign = self._require(self._campaigns, campaign_id, "campaign")
        self._campaigns[campaign_id] = campaign.model_copy(update={"state": "RUNNING"})
        now = datetime.now(UTC)
        return JobStatus(
            id=uuid4(), state=JobState.RUNNING, progress=0.0, processed=0, total=0, created_at=now
        )

    async def pause_campaign(self, campaign_id: str) -> CampaignResponse:
        campaign = self._require(self._campaigns, campaign_id, "campaign")
        paused = campaign.model_copy(update={"state": "PAUSED"})
        self._campaigns[campaign_id] = paused
        return paused

    # --- Resources ------------------------------------------------------------

    async def get_funnel(self, campaign_id: str) -> dict[str, int]:
        campaign = self._require(self._campaigns, campaign_id, "campaign")
        return {
            "discovered": campaign.discovered,
            "with_website": campaign.with_website,
            "person_found": campaign.person_found,
            "email_found": campaign.email_found,
            "verified": campaign.verified,
            "sent": campaign.sent,
            "replied": campaign.replied,
        }

    async def get_report(self, campaign_id: str) -> dict[str, object]:
        # get_funnel() below already validates campaign_id -- no need to
        # look it up twice.
        sources = await self.source_yield(campaign_id)
        return {
            "campaign_id": campaign_id,
            "funnel": await self.get_funnel(campaign_id),
            "reply_rate": 0.0,
            "sources": [s.model_dump(mode="json") for s in sources],
            "placement": {},
        }

    # --- helpers ------------------------------------------------------------------

    @staticmethod
    def _require(store: dict[str, T], key: str, kind: str) -> T:
        value = store.get(key)
        if value is None:
            raise ValueError(f"no {kind} with id {key!r}")
        return value


__all__ = ["InMemoryPlatformService"]
