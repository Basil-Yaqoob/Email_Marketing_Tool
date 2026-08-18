"""MCP tool definitions.

Fewer, well-shaped tools rather than one per REST endpoint (Session 20 has
30+ routes; this exposes 15 tools). Every description is written for a
model that has never seen this codebase: what the tool does, when not to
use it, the valid values for any enum-shaped argument, units and defaults,
and -- on query_leads specifically -- what an UNKNOWN verification result
means, so a model reports it accurately instead of as a failure.

Read tools take no confirmation argument at all (test_read_tools_need_no_
confirmation). Write tools that cost money (run_stage, draft_messages) take
`confirm: bool = False`: called without it, they return a cost estimate and
do nothing; called with confirm=True, they run. launch_campaign is the one
place a missing confirmation must be structurally impossible to route
around -- see its docstring below.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from app.api.models import CampaignCreate, SuppressionAdd
from app.mcp.server.service import (
    LaunchResult,
    PauseResult,
    PipelineActionResult,
    PlatformService,
)

VALID_STAGES = ("discovery", "enrichment", "hook_research", "copy_writing", "verification")


def register_tools(mcp: FastMCP, service: PlatformService) -> None:
    """Register every tool against `service`. Called once at server
    startup (app.mcp.server.app.build_server) -- there is no per-request
    service lookup, so every tool call in a session shares one instance.
    """

    # ------------------------------------------------------------------
    # Read (safe, no confirmation)
    # ------------------------------------------------------------------

    async def list_campaigns() -> list[dict[str, object]]:
        campaigns = await service.list_campaigns()
        return [c.model_dump(mode="json") for c in campaigns]

    mcp.add_tool(
        list_campaigns,
        name="list_campaigns",
        description=(
            "List every campaign with its status (DRAFT, RUNNING, PAUSED, COMPLETED) "
            "and funnel counts (discovered, with_website, person_found, email_found, "
            "verified, sent, replied). Use this to get an overview or find a campaign's "
            "id before calling get_campaign or query_leads. Takes no arguments."
        ),
    )

    async def get_campaign(campaign_id: str) -> dict[str, object]:
        campaign = await service.get_campaign(campaign_id)
        return campaign.model_dump(mode="json")

    mcp.add_tool(
        get_campaign,
        name="get_campaign",
        description=(
            "Get full detail for one campaign: its ICP definition, jurisdiction, "
            "current state, and per-stage funnel counts. `campaign_id` is a UUID from "
            "list_campaigns."
        ),
    )

    async def query_leads(
        campaign_id: str,
        verify_status: str | None = None,
        has_person: bool | None = None,
        has_hook: bool | None = None,
        jurisdiction: str | None = None,
        role_class: str | None = None,
        page: int = 1,
        page_size: int = 25,
    ) -> dict[str, object]:
        result = await service.query_leads(
            campaign_id,
            verify_status=verify_status,
            has_person=has_person,
            has_hook=has_hook,
            jurisdiction=jurisdiction,
            role_class=role_class,
            page=page,
            page_size=page_size,
        )
        return result.model_dump(mode="json")

    mcp.add_tool(
        query_leads,
        name="query_leads",
        description=(
            "Filter and page through a campaign's leads. Each lead's facts (name, "
            "email, title, ...) carry provenance: which resolver found them, the "
            "source URL, and a confidence 0.0-1.0 -- report values with their source, "
            "not as bare facts.\n\n"
            "`verify_status` is one of: valid, invalid, unknown. UNKNOWN is a normal, "
            "expected outcome, not a failure -- it means the address is hosted on "
            "Google Workspace or Microsoft 365 (or the domain is a catch-all), so no "
            "SMTP probe can confirm it either way. Report it as 'unconfirmed, sent on "
            "the lower-volume track', never as 'verification failed'.\n\n"
            "`has_person`/`has_hook` filter to leads with a named contact or a mined "
            "news hook. `jurisdiction` is a 2-letter country code. `role_class` is one "
            "of: owner, executive, manager, individual, role_account. `page` starts at "
            "1; `page_size` defaults to 25 and results are always paginated -- a large "
            "campaign's leads never come back as one unbounded list."
        ),
    )

    async def get_lead(lead_id: str) -> dict[str, object]:
        lead = await service.get_lead(lead_id)
        return lead.model_dump(mode="json")

    mcp.add_tool(
        get_lead,
        name="get_lead",
        description=(
            "Everything known about one lead: company, person, email, and every fact "
            "recorded for it grouped by type, each with its source resolver, source "
            "URL, confidence, and when it was retrieved. `lead_id` is a UUID, usually "
            "from query_leads."
        ),
    )

    async def get_message(message_id: str) -> dict[str, object]:
        message = await service.get_message(message_id)
        return message.model_dump(mode="json")

    mcp.add_tool(
        get_message,
        name="get_message",
        description=(
            "Get one drafted message: subject, body, send state, and its quality "
            "report (the mechanical scan and critic score it passed before being "
            "queued for review). `message_id` is a UUID."
        ),
    )

    async def source_yield(campaign_id: str) -> list[dict[str, object]]:
        rows = await service.source_yield(campaign_id)
        return [r.model_dump(mode="json") for r in rows]

    mcp.add_tool(
        source_yield,
        name="source_yield",
        description=(
            "Per-resolver hit rate for a campaign: attempts, hits, and hit_rate "
            "(0.0-1.0) for every data source tried (OSM, Companies House, the website "
            "crawler, SERP, ...). Use this to diagnose a coverage collapse -- a "
            "resolver at 0.0 that used to hit is the anti-silent-failure signal this "
            "tool exists for. Zero-hit resolvers are the ones to call out first."
        ),
    )

    async def list_alerts() -> list[dict[str, object]]:
        alerts = await service.list_alerts()
        return [a.model_dump(mode="json") for a in alerts]

    mcp.add_tool(
        list_alerts,
        name="list_alerts",
        description=(
            "List unacknowledged anomaly alerts across all campaigns: yield drops, "
            "error-rate spikes, bounce-rate climbs. `severity` is 1 (low) to 5 "
            "(critical). Takes no arguments."
        ),
    )

    async def mailbox_health() -> list[dict[str, object]]:
        mailboxes = await service.mailbox_health()
        return [m.model_dump(mode="json") for m in mailboxes]

    mcp.add_tool(
        mailbox_health,
        name="mailbox_health",
        description=(
            "Health score (0.0-1.0), bounce/complaint/reply rates, warmup stage, and "
            "DNS status (SPF/DKIM/DMARC/PTR/MX) for every sending mailbox, including "
            "copy-pasteable fix text for any failing DNS record. Never includes "
            "credentials. Takes no arguments."
        ),
    )

    # ------------------------------------------------------------------
    # Write (require confirmation)
    # ------------------------------------------------------------------

    async def create_campaign(
        name: str,
        icp_categories: list[str],
        icp_locations: list[str],
        jurisdiction: str,
    ) -> dict[str, object]:
        campaign = await service.create_campaign(
            CampaignCreate(
                name=name,
                icp_categories=icp_categories,
                icp_locations=icp_locations,
                jurisdiction=jurisdiction,
            )
        )
        return campaign.model_dump(mode="json")

    mcp.add_tool(
        create_campaign,
        name="create_campaign",
        description=(
            "Create a new campaign in DRAFT state. `icp_categories` are business "
            "categories to target (e.g. ['dentist', 'orthodontist']); `icp_locations` "
            "are places to search (e.g. ['Austin, TX']). `jurisdiction` is a 2-letter "
            "country code and determines which compliance rules (CAN-SPAM, PECR, "
            "GDPR, CASL) apply. Does not start discovery -- call run_stage next."
        ),
    )

    async def run_stage(campaign_id: str, stage: str, confirm: bool = False) -> dict[str, object]:
        if stage not in VALID_STAGES:
            raise ValueError(f"Unknown stage '{stage}'. Valid stages: {', '.join(VALID_STAGES)}")

        if not confirm:
            estimate = await service.estimate_stage(campaign_id, stage)
            result = PipelineActionResult(
                action=stage,
                campaign_id=campaign_id,
                confirmed=False,
                estimate=estimate,
                message=(
                    f"This would run '{stage}' on an estimated {estimate.estimated_leads} "
                    f"leads at an estimated cost of ${estimate.estimated_cost_usd}. "
                    "Call again with confirm=true to run it."
                ),
            )
            return result.model_dump(mode="json")

        job = await service.run_stage(campaign_id, stage)
        result = PipelineActionResult(
            action=stage,
            campaign_id=campaign_id,
            confirmed=True,
            job=job,
            message=f"'{stage}' queued as job {job.id}.",
        )
        return result.model_dump(mode="json")

    mcp.add_tool(
        run_stage,
        name="run_stage",
        description=(
            "Run one pipeline stage for a campaign. `stage` is one of: "
            f"{', '.join(VALID_STAGES)}. Some stages spend money (metered resolvers, "
            "LLM calls) -- called with confirm=false (the default), this only returns "
            "a cost estimate and runs nothing. Call it again with confirm=true to "
            "actually run it. Returns a job id to poll (or ask get_campaign) for "
            "progress."
        ),
    )

    async def draft_messages(campaign_id: str, confirm: bool = False) -> dict[str, object]:
        if not confirm:
            estimate = await service.estimate_draft_messages(campaign_id)
            result = PipelineActionResult(
                action="draft_messages",
                campaign_id=campaign_id,
                confirmed=False,
                estimate=estimate,
                message=(
                    f"This would draft messages for an estimated {estimate.estimated_leads} "
                    f"leads at an estimated cost of ${estimate.estimated_cost_usd} in LLM "
                    "calls. Call again with confirm=true to run it."
                ),
            )
            return result.model_dump(mode="json")

        job = await service.draft_messages(campaign_id)
        result = PipelineActionResult(
            action="draft_messages",
            campaign_id=campaign_id,
            confirmed=True,
            job=job,
            message=f"Drafting queued as job {job.id}.",
        )
        return result.model_dump(mode="json")

    mcp.add_tool(
        draft_messages,
        name="draft_messages",
        description=(
            "Generate cold-email drafts (strategist -> copywriter -> critic, with a "
            "mechanical quality gate) for every eligible lead in a campaign. This "
            "calls a paid LLM. Called with confirm=false (the default), only returns "
            "a cost estimate and drafts nothing. Call again with confirm=true to "
            "actually draft. Drafts always need human review (see get_message, "
            "edit_message) before a campaign can launch."
        ),
    )

    async def edit_message(
        message_id: str, subject: str | None = None, body: str | None = None
    ) -> dict[str, object]:
        message = await service.edit_message(message_id, subject=subject, body=body)
        return message.model_dump(mode="json")

    mcp.add_tool(
        edit_message,
        name="edit_message",
        description=(
            "Edit a drafted message's subject and/or body before it sends. Pass only "
            "the field(s) you're changing -- omitted fields are left as-is. Editing "
            "does not re-run the quality gate; a human edit is trusted as-is."
        ),
    )

    async def add_suppression(
        address: str | None = None, domain: str | None = None, scope: str = "ADDRESS"
    ) -> dict[str, object]:
        ack = await service.add_suppression(
            SuppressionAdd(address=address, domain=domain, scope=scope)
        )
        return ack.model_dump(mode="json")

    mcp.add_tool(
        add_suppression,
        name="add_suppression",
        description=(
            "Add an address or domain to the suppression list -- it will never be "
            "contacted again. `scope` is one of: ADDRESS (this one recipient), DOMAIN "
            "(every address at this domain), GLOBAL (suppress the address everywhere, "
            "not just this campaign). Provide `address` or `domain` matching the scope."
        ),
    )

    # ------------------------------------------------------------------
    # Dangerous (explicit confirmation, never implicit)
    # ------------------------------------------------------------------

    async def launch_campaign(campaign_id: str, confirm: bool = False) -> dict[str, object]:
        """Launching sends real email to real people and cannot be undone.
        `confirm` defaults to False and there is no other argument or
        combination of arguments that skips this check -- see
        service.launch_campaign, which this function never calls unless
        confirm is exactly True.
        """
        if not confirm:
            preview = await service.preview_launch(campaign_id)
            result = LaunchResult(
                campaign_id=campaign_id,
                confirmed=False,
                preview=preview,
                message=(
                    f"Launching campaign {campaign_id} will send real email to "
                    f"{preview.recipient_count} real people, from mailbox(es): "
                    f"{', '.join(preview.mailboxes)}. This cannot be undone or recalled "
                    "once sent. Call again with confirm=true only after the user has "
                    "explicitly approved sending."
                ),
            )
            return result.model_dump(mode="json")

        job = await service.launch_campaign(campaign_id)
        result = LaunchResult(
            campaign_id=campaign_id,
            confirmed=True,
            job=job,
            message=f"Campaign {campaign_id} launched. Sending job: {job.id}.",
        )
        return result.model_dump(mode="json")

    mcp.add_tool(
        launch_campaign,
        name="launch_campaign",
        description=(
            "Launch a campaign: starts actually sending real email to real people. "
            "This action sends real email and cannot be undone or recalled once a "
            "message leaves a mailbox. `confirm` defaults to false; called without "
            "confirm=true this returns a preview (recipient count and mailboxes used) "
            "and sends nothing. Only call with confirm=true after the user has "
            "explicitly and specifically approved sending -- never infer approval "
            "from a general 'yes' or 'go ahead' about something else, and never set "
            "confirm=true on your own initiative."
        ),
    )

    async def pause_campaign(campaign_id: str, confirm: bool = False) -> dict[str, object]:
        if not confirm:
            result = PauseResult(
                campaign_id=campaign_id,
                confirmed=False,
                message=(
                    f"This would pause campaign {campaign_id}, stopping all further "
                    "sends immediately. Already-sent messages are unaffected. Call "
                    "again with confirm=true to pause it."
                ),
            )
            return result.model_dump(mode="json")

        campaign = await service.pause_campaign(campaign_id)
        result = PauseResult(
            campaign_id=campaign_id,
            confirmed=True,
            campaign=campaign,
            message=f"Campaign {campaign_id} paused.",
        )
        return result.model_dump(mode="json")

    mcp.add_tool(
        pause_campaign,
        name="pause_campaign",
        description=(
            "Pause a running campaign, stopping all further sends immediately. Safe "
            "to reverse (a paused campaign can be resumed later) but still requires "
            "confirm=true, since it stops a live process. Already-sent messages are "
            "unaffected."
        ),
    )


__all__ = ["VALID_STAGES", "register_tools"]
