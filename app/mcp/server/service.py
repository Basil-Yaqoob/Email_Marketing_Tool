"""The service layer the MCP server calls.

Session 23's plan is explicit: tools call "the Session 20 service layer --
not the HTTP API over localhost, and not the repositories directly." Session
20 shipped route handlers directly, with no separate service module to
import (see doc/02-ROADMAP.md's Discovered work table for the same gap
recorded against every session since 03: the mechanism is built and tested,
nothing has wired it to a real campaign yet). Rather than have this server
either loop back through its own REST API over HTTP -- the one thing the
plan explicitly forbids -- or reach into repositories directly -- the other
thing it forbids -- PlatformService is defined here as the boundary the
plan describes. A future orchestration session gives Session 20's routes.py
a real implementation of the same Protocol and both transports genuinely
share it, exactly as planned; until then, this Protocol is the seam.

Every method returns an app.api.models type (or a type defined alongside
this Protocol for tool-shaped results like LaunchPreview) -- the same
boundary types the REST API returns, so a client sees one consistent shape
regardless of transport.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field

from app.api.models import (
    AlertResponse,
    CampaignCreate,
    CampaignResponse,
    JobStatus,
    LeadResponse,
    LeadsListResponse,
    MailboxResponse,
    MessageResponse,
    SuppressionAdd,
)


class SourceYieldRow(BaseModel):
    """One resolver's hit rate for one campaign -- lets a client diagnose a
    collapse (Session 19's per-source yield dashboard, over MCP).
    """

    resolver: str
    field: str
    tier: str
    attempts: int
    hits: int
    hit_rate: float = Field(ge=0.0, le=1.0)


class CostEstimate(BaseModel):
    """What a metered pipeline stage is projected to cost, shown before it
    runs -- CLAUDE.md rule 2.3, over MCP this time instead of the CLI/API.
    """

    campaign_id: str
    stage: str
    estimated_leads: int
    estimated_cost_usd: str  # Decimal as str -- exact, and plain JSON


class PipelineActionResult(BaseModel):
    """Result of a write tool that costs money (run_stage, draft_messages).

    confirmed=False means nothing ran: `estimate` is populated and `job` is
    None. confirmed=True means the stage was queued: `job` is populated.
    Calling the tool a second time with confirm=True is what makes it run.
    """

    action: str
    campaign_id: str
    confirmed: bool
    estimate: CostEstimate | None = None
    job: JobStatus | None = None
    message: str


class LaunchPreview(BaseModel):
    """What launch_campaign will do, shown before it's allowed to run."""

    campaign_id: str
    recipient_count: int
    mailboxes: list[str]


class LaunchResult(BaseModel):
    """Result of launch_campaign.

    confirmed=False (the default, and the only safe path without an
    explicit argument) never sends anything -- `preview` describes what a
    confirmed call would do, `job` is None. confirmed=True is the one path
    that sends real email; `job` is populated and `preview` is None.
    """

    campaign_id: str
    confirmed: bool
    preview: LaunchPreview | None = None
    job: JobStatus | None = None
    message: str


class PauseResult(BaseModel):
    campaign_id: str
    confirmed: bool
    campaign: CampaignResponse | None = None
    message: str


class SuppressionAck(BaseModel):
    address: str | None
    domain: str | None
    scope: str


class PlatformService(Protocol):
    """Everything the MCP tools (app.mcp.server.tools) call. See module
    docstring for why this is a Protocol rather than a concrete class here.
    """

    # --- Read ---------------------------------------------------------------
    async def list_campaigns(self) -> list[CampaignResponse]: ...
    async def get_campaign(self, campaign_id: str) -> CampaignResponse: ...
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
    ) -> LeadsListResponse: ...
    async def get_lead(self, lead_id: str) -> LeadResponse: ...
    async def get_message(self, message_id: str) -> MessageResponse: ...
    async def source_yield(self, campaign_id: str) -> list[SourceYieldRow]: ...
    async def list_alerts(self) -> list[AlertResponse]: ...
    async def mailbox_health(self) -> list[MailboxResponse]: ...

    # --- Write ----------------------------------------------------------------
    async def create_campaign(self, data: CampaignCreate) -> CampaignResponse: ...
    async def estimate_stage(self, campaign_id: str, stage: str) -> CostEstimate: ...
    async def run_stage(self, campaign_id: str, stage: str) -> JobStatus: ...
    async def estimate_draft_messages(self, campaign_id: str) -> CostEstimate: ...
    async def draft_messages(self, campaign_id: str) -> JobStatus: ...
    async def edit_message(
        self, message_id: str, *, subject: str | None = None, body: str | None = None
    ) -> MessageResponse: ...
    async def add_suppression(self, data: SuppressionAdd) -> SuppressionAck: ...

    # --- Dangerous --------------------------------------------------------------
    async def preview_launch(self, campaign_id: str) -> LaunchPreview: ...
    async def launch_campaign(self, campaign_id: str) -> JobStatus: ...
    async def pause_campaign(self, campaign_id: str) -> CampaignResponse: ...

    # --- Resources ------------------------------------------------------------
    async def get_funnel(self, campaign_id: str) -> dict[str, int]: ...
    async def get_report(self, campaign_id: str) -> dict[str, object]: ...


__all__ = [
    "CostEstimate",
    "LaunchPreview",
    "LaunchResult",
    "PauseResult",
    "PipelineActionResult",
    "PlatformService",
    "SourceYieldRow",
    "SuppressionAck",
]
