"""Unit tests for the MCP server (Session 23).

22 tests covering:
  - stdio and Streamable HTTP transport startup
  - Tool schema validity and descriptions
  - launch_campaign's confirmation gate (load-bearing: tests 5-6)
  - run_stage/draft_messages' cost-estimate-before-running gate
  - query_leads provenance and its UNKNOWN-status explanation
  - Structured content, not prose
  - The service-layer boundary (no HTTP loopback, no direct repository access)
  - Streamable HTTP auth vs. stdio's no-auth
  - Resources (campaign funnel, docs://limits)
  - MCP errors, not raised exceptions
  - No tool ever returns credentials
  - ABORTED jobs surface their reason through MCP, consistent with Session 20

Every test drives a real FastMCP server through the real MCP protocol --
either the SDK's own in-memory client/server session helper (stdio-
equivalent, no subprocess) or, for the one true stdio test, a real
subprocess of the documented `python -m app.mcp.server` command. Nothing
here hand-calls tool functions directly; assertions go through
ClientSession.call_tool()/read_resource(), the same path a real client uses.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import jsonschema
import pytest
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session
from pydantic import AnyUrl
from starlette.testclient import TestClient

from app.api.models import (
    AlertResponse,
    CampaignCreate,
    CampaignResponse,
    FactRecord,
    JobError,
    JobState,
    JobStatus,
    LeadResponse,
    LeadsListResponse,
    MailboxResponse,
    MessageResponse,
    SuppressionAdd,
)
from app.core.config import Settings
from app.mcp.server.app import build_server, build_streamable_http_app, run_stdio
from app.mcp.server.memory_service import InMemoryPlatformService
from app.mcp.server.service import (
    CostEstimate,
    LaunchPreview,
    SourceYieldRow,
    SuppressionAck,
)

# No module-level asyncio mark: pyproject.toml sets asyncio_mode = "auto".


# ============================================================================
# A minimal, fully-controllable service double
# ============================================================================


class FakePlatformService:
    """Implements only what a given test exercises; anything unused raises
    NotImplementedError with a clear message rather than silently returning
    a placeholder (CLAUDE.md rule 2.1).
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.campaign = _campaign()
        self.lead = _lead(self.campaign.id)
        self.cost_estimate = CostEstimate(
            campaign_id=str(self.campaign.id),
            stage="enrichment",
            estimated_leads=10,
            estimated_cost_usd="2.50",
        )
        self.launch_preview = LaunchPreview(
            campaign_id=str(self.campaign.id), recipient_count=10, mailboxes=["a@sender.example"]
        )
        self.run_stage_job: JobStatus | None = None
        self.launch_job: JobStatus | None = None
        self.source_yield_rows: list[SourceYieldRow] = []

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    def call_count(self, name: str) -> int:
        return sum(1 for call_name, _, _ in self.calls if call_name == name)

    async def list_campaigns(self) -> list[CampaignResponse]:
        self._record("list_campaigns")
        return [self.campaign]

    async def get_campaign(self, campaign_id: str) -> CampaignResponse:
        self._record("get_campaign", campaign_id)
        return self.campaign

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
        self._record(
            "query_leads",
            campaign_id,
            verify_status=verify_status,
            has_person=has_person,
            has_hook=has_hook,
            jurisdiction=jurisdiction,
            role_class=role_class,
            page=page,
            page_size=page_size,
        )
        return LeadsListResponse(leads=[self.lead], total=1, page=page, page_size=page_size)

    async def get_lead(self, lead_id: str) -> LeadResponse:
        self._record("get_lead", lead_id)
        return self.lead

    async def get_message(self, message_id: str) -> MessageResponse:
        raise NotImplementedError

    async def source_yield(self, campaign_id: str) -> list[SourceYieldRow]:
        self._record("source_yield", campaign_id)
        return self.source_yield_rows

    async def list_alerts(self) -> list[AlertResponse]:
        raise NotImplementedError

    async def mailbox_health(self) -> list[MailboxResponse]:
        return [_mailbox()]

    async def create_campaign(self, data: CampaignCreate) -> CampaignResponse:
        raise NotImplementedError

    async def estimate_stage(self, campaign_id: str, stage: str) -> CostEstimate:
        self._record("estimate_stage", campaign_id, stage)
        return self.cost_estimate

    async def run_stage(self, campaign_id: str, stage: str) -> JobStatus:
        self._record("run_stage", campaign_id, stage)
        if self.run_stage_job is not None:
            return self.run_stage_job
        return JobStatus(
            id=uuid4(),
            state=JobState.RUNNING,
            progress=0.0,
            processed=0,
            total=10,
            created_at=datetime.now(UTC),
        )

    async def estimate_draft_messages(self, campaign_id: str) -> CostEstimate:
        raise NotImplementedError

    async def draft_messages(self, campaign_id: str) -> JobStatus:
        raise NotImplementedError

    async def edit_message(
        self, message_id: str, *, subject: str | None = None, body: str | None = None
    ) -> MessageResponse:
        raise NotImplementedError

    async def add_suppression(self, data: SuppressionAdd) -> SuppressionAck:
        raise NotImplementedError

    async def preview_launch(self, campaign_id: str) -> LaunchPreview:
        self._record("preview_launch", campaign_id)
        return self.launch_preview

    async def launch_campaign(self, campaign_id: str) -> JobStatus:
        self._record("launch_campaign", campaign_id)
        if self.launch_job is not None:
            return self.launch_job
        return JobStatus(
            id=uuid4(),
            state=JobState.RUNNING,
            progress=0.0,
            processed=0,
            total=10,
            created_at=datetime.now(UTC),
        )

    async def pause_campaign(self, campaign_id: str) -> CampaignResponse:
        raise NotImplementedError

    async def get_funnel(self, campaign_id: str) -> dict[str, int]:
        raise NotImplementedError

    async def get_report(self, campaign_id: str) -> dict[str, object]:
        raise NotImplementedError


def _campaign(**overrides: object) -> CampaignResponse:
    now = datetime.now(UTC)
    defaults: dict[str, object] = {
        "id": uuid4(),
        "name": "Test Campaign",
        "icp_categories": ["dentist"],
        "icp_locations": ["Austin, TX"],
        "icp_filters": {},
        "jurisdiction": "US",
        "state": "DRAFT",
        "discovered": 10,
        "with_website": 8,
        "person_found": 6,
        "email_found": 5,
        "verified": 3,
        "sent": 0,
        "replied": 0,
        "created_at": now,
        "updated_at": now,
    }
    defaults.update(overrides)
    return CampaignResponse(**defaults)


def _lead(campaign_id: object, **overrides: object) -> LeadResponse:
    now = datetime.now(UTC)
    defaults: dict[str, object] = {
        "id": uuid4(),
        "campaign_id": campaign_id,
        "company_name": "Riverside Dental",
        "website_url": "https://riversidedental.example",
        "person_name": "Dr. Jane Smith",
        "person_title": "Owner",
        "email": "jane@riversidedental.example",
        "email_verified": "unknown",
        "facts": {
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
        "created_at": now,
    }
    defaults.update(overrides)
    return LeadResponse(**defaults)


def _mailbox(**overrides: object) -> MailboxResponse:
    defaults: dict[str, object] = {
        "id": uuid4(),
        "address": "outreach@example-sender.com",
        "health_score": 0.9,
        "bounce_rate": 0.01,
        "complaint_rate": 0.0,
        "reply_rate": 0.03,
        "warmup_stage": 5,
        "dns_status": {"spf": "PASS"},
        "dns_fix_text": {},
        "created_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return MailboxResponse(**defaults)


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "_env_file": None,
        "database_url": "postgresql+asyncpg://u:p@localhost/db",
        "redis_url": "redis://localhost:6379/0",
        "secret_key": "test-secret-key",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def _structured(result: types.CallToolResult) -> dict[str, Any]:
    """Narrow structuredContent (typed dict[str, Any] | None on
    CallToolResult) for tests -- every tool here always returns structured
    content, so a None here is itself a test failure, not a case to handle.
    """
    assert result.structuredContent is not None
    return result.structuredContent


# ============================================================================
# Transport startup (1-2)
# ============================================================================


async def test_server_starts_over_stdio() -> None:
    """The exact documented command (`python -m app.mcp.server`) spawned as
    a real subprocess, connected to with a real ClientSession over real
    stdio pipes -- the same path Claude Desktop/Code use.
    """
    params = StdioServerParameters(command=sys.executable, args=["-m", "app.mcp.server"])

    async def _run() -> None:
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert len(tools.tools) == 15
            result = await session.call_tool("list_campaigns", {})
            assert result.isError is False

    await asyncio.wait_for(_run(), timeout=20.0)


async def test_server_starts_over_streamable_http() -> None:
    """The Streamable HTTP app assembles, its ASGI lifespan starts the
    session manager cleanly, and a valid-token request reaches the real MCP
    protocol layer (evidenced by getting past auth to a protocol-level
    response instead of a 401/421).
    """
    settings = _settings(api_token="secret-token")
    service = InMemoryPlatformService()
    app = build_streamable_http_app(service, settings)

    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "ping", "id": 1},
            headers={
                "Authorization": "Bearer secret-token",
                "Accept": "application/json, text/event-stream",
            },
        )
    # Never 401 (auth passed) or 421 (host rejected) -- whatever comes back
    # is the real protocol handler's response, not the auth gate's.
    assert response.status_code not in (401, 421)


# ============================================================================
# Schema and description tests (3-4)
# ============================================================================


async def test_all_tool_schemas_are_valid_json_schema() -> None:
    server = build_server(FakePlatformService())
    async with create_connected_server_and_client_session(server, raise_exceptions=True) as client:
        tools = await client.list_tools()

    assert len(tools.tools) == 15
    for tool in tools.tools:
        jsonschema.Draft7Validator.check_schema(tool.inputSchema)


async def test_every_tool_has_a_description() -> None:
    server = build_server(FakePlatformService())
    async with create_connected_server_and_client_session(server, raise_exceptions=True) as client:
        tools = await client.list_tools()

    for tool in tools.tools:
        assert tool.description is not None
        assert len(tool.description.strip()) > 20, f"{tool.name} needs a real description"


# ============================================================================
# The confirmation gate (5-8) -- load-bearing
# ============================================================================


async def test_launch_campaign_requires_explicit_confirm() -> None:
    fake = FakePlatformService()
    server = build_server(fake)
    campaign_id = str(fake.campaign.id)

    async with create_connected_server_and_client_session(server, raise_exceptions=True) as client:
        no_arg = await client.call_tool("launch_campaign", {"campaign_id": campaign_id})
        explicit_false = await client.call_tool(
            "launch_campaign", {"campaign_id": campaign_id, "confirm": False}
        )
        confirmed = await client.call_tool(
            "launch_campaign", {"campaign_id": campaign_id, "confirm": True}
        )

    no_arg_data = _structured(no_arg)
    assert no_arg_data["confirmed"] is False
    assert no_arg_data["job"] is None
    assert _structured(explicit_false)["confirmed"] is False
    confirmed_data = _structured(confirmed)
    assert confirmed_data["confirmed"] is True
    assert confirmed_data["job"] is not None

    # The real send-path (service.launch_campaign) must only have been
    # reached on the one confirmed call.
    assert fake.call_count("launch_campaign") == 1
    assert fake.call_count("preview_launch") == 2  # the two unconfirmed calls


async def test_launch_campaign_description_states_it_sends_real_email() -> None:
    server = build_server(FakePlatformService())
    async with create_connected_server_and_client_session(server, raise_exceptions=True) as client:
        tools = await client.list_tools()

    launch_tool = next(t for t in tools.tools if t.name == "launch_campaign")
    description = launch_tool.description.lower()  # type: ignore[union-attr]
    assert "real email" in description
    assert "cannot be undone" in description or "cannot be recalled" in description


async def test_run_stage_returns_cost_estimate_before_running() -> None:
    fake = FakePlatformService()
    server = build_server(fake)
    campaign_id = str(fake.campaign.id)

    async with create_connected_server_and_client_session(server, raise_exceptions=True) as client:
        preview = await client.call_tool(
            "run_stage", {"campaign_id": campaign_id, "stage": "enrichment"}
        )

    preview_data = _structured(preview)
    assert preview_data["confirmed"] is False
    assert preview_data["estimate"]["estimated_cost_usd"] == "2.50"
    assert preview_data["job"] is None
    assert fake.call_count("run_stage") == 0  # nothing actually ran


async def test_read_tools_need_no_confirmation() -> None:
    read_tools = {
        "list_campaigns",
        "get_campaign",
        "query_leads",
        "get_lead",
        "get_message",
        "source_yield",
        "list_alerts",
        "mailbox_health",
    }
    server = build_server(FakePlatformService())
    async with create_connected_server_and_client_session(server, raise_exceptions=True) as client:
        tools = await client.list_tools()

    for tool in tools.tools:
        if tool.name in read_tools:
            assert "confirm" not in tool.inputSchema.get("properties", {}), tool.name


# ============================================================================
# query_leads / get_lead provenance (9-12)
# ============================================================================


async def test_query_leads_filters_apply() -> None:
    fake = FakePlatformService()
    server = build_server(fake)
    campaign_id = str(fake.campaign.id)

    async with create_connected_server_and_client_session(server, raise_exceptions=True) as client:
        await client.call_tool(
            "query_leads",
            {
                "campaign_id": campaign_id,
                "verify_status": "unknown",
                "has_person": True,
                "jurisdiction": "US",
                "page": 2,
                "page_size": 10,
            },
        )

    _, args, kwargs = fake.calls[-1]
    assert args == (campaign_id,)
    assert kwargs["verify_status"] == "unknown"
    assert kwargs["has_person"] is True
    assert kwargs["jurisdiction"] == "US"
    assert kwargs["page"] == 2
    assert kwargs["page_size"] == 10


async def test_query_leads_returns_provenance() -> None:
    service = InMemoryPlatformService()
    server = build_server(service)
    campaign_id = next(iter(service._campaigns))

    async with create_connected_server_and_client_session(server, raise_exceptions=True) as client:
        result = await client.call_tool("query_leads", {"campaign_id": campaign_id})

    lead = _structured(result)["leads"][0]
    fact = lead["facts"]["person_name"][0]
    assert fact["source"] == "website_crawler"
    assert fact["source_url"].startswith("https://")
    assert 0.0 <= fact["confidence"] <= 1.0
    assert "retrieved_at" in fact


async def test_query_leads_description_explains_unknown_status() -> None:
    server = build_server(FakePlatformService())
    async with create_connected_server_and_client_session(server, raise_exceptions=True) as client:
        tools = await client.list_tools()

    query_tool = next(t for t in tools.tools if t.name == "query_leads")
    description = query_tool.description.lower()  # type: ignore[union-attr]
    assert "unknown" in description
    assert "google workspace" in description or "microsoft 365" in description
    assert "not" in description
    assert "failure" in description


async def test_get_lead_includes_source_urls() -> None:
    fake = FakePlatformService()
    server = build_server(fake)

    async with create_connected_server_and_client_session(server, raise_exceptions=True) as client:
        result = await client.call_tool("get_lead", {"lead_id": str(fake.lead.id)})

    fact = _structured(result)["facts"]["person_name"][0]
    assert fact["source_url"] == "https://riversidedental.example/about"


# ============================================================================
# Diagnostics, structured content, layering (13-15)
# ============================================================================


async def test_source_yield_tool_surfaces_zero_hit_resolvers() -> None:
    fake = FakePlatformService()
    fake.source_yield_rows = [
        SourceYieldRow(
            resolver="osm", field="company", tier="FREE", attempts=40, hits=40, hit_rate=1.0
        ),
        SourceYieldRow(
            resolver="serp_person",
            field="person_name",
            tier="FREE",
            attempts=40,
            hits=0,
            hit_rate=0.0,
        ),
    ]
    server = build_server(fake)

    async with create_connected_server_and_client_session(server, raise_exceptions=True) as client:
        result = await client.call_tool("source_yield", {"campaign_id": str(fake.campaign.id)})

    rows = _structured(result)["result"]
    zero_hit = [r for r in rows if r["hit_rate"] == 0.0]
    assert len(zero_hit) == 1
    assert zero_hit[0]["resolver"] == "serp_person"


async def test_tools_return_structured_content_not_prose() -> None:
    fake = FakePlatformService()
    server = build_server(fake)

    async with create_connected_server_and_client_session(server, raise_exceptions=True) as client:
        list_result = await client.call_tool("list_campaigns", {})
        get_result = await client.call_tool("get_campaign", {"campaign_id": str(fake.campaign.id)})

    list_data = _structured(list_result)
    get_data = _structured(get_result)
    assert list_data is not None
    # Structured content is real data, not a JSON blob wrapped in prose.
    assert isinstance(get_data, dict)
    assert get_data["name"] == fake.campaign.name


def test_server_uses_service_layer_not_http_loopback() -> None:
    """Structural guard: app.mcp.server.tools must call the injected
    PlatformService, never loop back over its own REST API (which the plan
    explicitly forbids) or reach into repositories directly.
    """
    import inspect

    from app.mcp.server import tools as tools_module

    source = inspect.getsource(tools_module)
    assert "httpx" not in source
    assert "requests" not in source
    assert "app.api.routes" not in source
    assert "app.db.repositories" not in source
    assert "localhost" not in source


# ============================================================================
# Auth (16-17)
# ============================================================================


def test_streamable_http_requires_auth() -> None:
    settings = _settings(api_token="secret-token")
    service = InMemoryPlatformService()
    app = build_streamable_http_app(service, settings)

    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        missing = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "ping", "id": 1},
            headers={"Accept": "application/json, text/event-stream"},
        )
        wrong = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "ping", "id": 1},
            headers={
                "Authorization": "Bearer wrong-token",
                "Accept": "application/json, text/event-stream",
            },
        )

    assert missing.status_code == 401
    assert wrong.status_code == 401


async def test_stdio_needs_no_auth() -> None:
    """run_stdio takes no token/settings argument at all -- there is
    structurally nothing to configure wrong. And a real connection over the
    documented command succeeds with zero credentials presented.
    """
    import inspect

    signature = inspect.signature(run_stdio)
    assert "token" not in signature.parameters
    assert "settings" not in signature.parameters

    server = build_server(FakePlatformService())
    async with create_connected_server_and_client_session(server, raise_exceptions=True) as client:
        result = await client.call_tool("list_campaigns", {})
    assert result.isError is False


# ============================================================================
# Resources (18-19)
# ============================================================================


async def test_resource_funnel_returns_current_numbers() -> None:
    service = InMemoryPlatformService()
    server = build_server(service)
    campaign_id = next(iter(service._campaigns))
    expected = await service.get_funnel(campaign_id)

    async with create_connected_server_and_client_session(server, raise_exceptions=True) as client:
        result = await client.read_resource(AnyUrl(f"campaign://{campaign_id}/funnel"))

    payload = json.loads(result.contents[0].text)  # type: ignore[union-attr]
    assert payload == expected


async def test_docs_limits_resource_lists_hard_limits() -> None:
    server = build_server(FakePlatformService())
    async with create_connected_server_and_client_session(server, raise_exceptions=True) as client:
        result = await client.read_resource(AnyUrl("docs://limits"))

    text = result.contents[0].text  # type: ignore[union-attr]
    assert "55-70%" in text
    assert "Google Workspace" in text
    assert "linkedin.com" in text
    assert "SendGrid" in text


# ============================================================================
# Errors and credentials (20-22)
# ============================================================================


async def test_errors_returned_as_mcp_errors_not_exceptions() -> None:
    server = build_server(FakePlatformService())
    async with create_connected_server_and_client_session(server, raise_exceptions=False) as client:
        # get_message always raises NotImplementedError in FakePlatformService.
        result = await client.call_tool("get_message", {"message_id": "anything"})

    assert result.isError is True
    assert len(result.content) > 0


async def test_credentials_never_returned_by_any_tool() -> None:
    service = InMemoryPlatformService()
    server = build_server(service)
    campaign_id = next(iter(service._campaigns))

    forbidden = ("password", "smtp_password", "api_key", "credential", "secret")
    calls: list[tuple[str, dict[str, Any]]] = [
        ("list_campaigns", {}),
        ("get_campaign", {"campaign_id": campaign_id}),
        ("query_leads", {"campaign_id": campaign_id}),
        ("mailbox_health", {}),
        ("source_yield", {"campaign_id": campaign_id}),
    ]

    async with create_connected_server_and_client_session(server, raise_exceptions=True) as client:
        for name, args in calls:
            result = await client.call_tool(name, args)
            blob = json.dumps(_structured(result)).lower()
            for word in forbidden:
                assert word not in blob, f"{name} leaked {word!r}"


async def test_aborted_job_reason_surfaces_through_mcp() -> None:
    """Consistency with Session 20: an ABORTED job (guard fired) carries
    its reason verbatim, distinct from a crashed FAILED job -- and that
    distinction survives the trip through MCP's structured content.
    """
    fake = FakePlatformService()
    reason = "error rate 8.0% exceeded threshold. Last error: Resolver timeout"
    fake.run_stage_job = JobStatus(
        id=uuid4(),
        state=JobState.ABORTED,
        progress=0.25,
        processed=25,
        total=100,
        errors=[JobError(code="batch_aborted", message=reason)],
        aborted_reason=reason,
        created_at=datetime.now(UTC),
    )
    server = build_server(fake)

    async with create_connected_server_and_client_session(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "run_stage",
            {"campaign_id": str(fake.campaign.id), "stage": "enrichment", "confirm": True},
        )

    job = _structured(result)["job"]
    assert job["state"] == "aborted"
    assert job["aborted_reason"] == reason


# ============================================================================
# Gap-closing tests: the remaining write-tool paths against a real
# InMemoryPlatformService, not just FakePlatformService's canned returns --
# exercises create_campaign, draft_messages, edit_message, add_suppression,
# pause_campaign, and get_report end to end through the real MCP protocol.
# ============================================================================


async def test_create_campaign_tool_creates_a_real_campaign() -> None:
    service = InMemoryPlatformService()
    server = build_server(service)

    async with create_connected_server_and_client_session(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "create_campaign",
            {
                "name": "New Campaign",
                "icp_categories": ["orthodontist"],
                "icp_locations": ["Denver, CO"],
                "jurisdiction": "US",
            },
        )
        data = _structured(result)
        assert data["name"] == "New Campaign"
        assert data["state"] == "DRAFT"

        listed = await client.call_tool("list_campaigns", {})
    names = {c["name"] for c in _structured(listed)["result"]}
    assert "New Campaign" in names


async def test_draft_messages_confirmation_gate_against_real_service() -> None:
    service = InMemoryPlatformService()
    server = build_server(service)
    campaign_id = next(iter(service._campaigns))

    async with create_connected_server_and_client_session(server, raise_exceptions=True) as client:
        preview = await client.call_tool("draft_messages", {"campaign_id": campaign_id})
        confirmed = await client.call_tool(
            "draft_messages", {"campaign_id": campaign_id, "confirm": True}
        )

    preview_data = _structured(preview)
    assert preview_data["confirmed"] is False
    assert preview_data["job"] is None
    assert float(preview_data["estimate"]["estimated_cost_usd"]) > 0

    confirmed_data = _structured(confirmed)
    assert confirmed_data["confirmed"] is True
    assert confirmed_data["job"]["state"] == "queued"


async def test_edit_message_persists_against_real_service() -> None:
    service = InMemoryPlatformService()
    server = build_server(service)
    message_id = next(iter(service._messages))

    async with create_connected_server_and_client_session(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "edit_message", {"message_id": message_id, "subject": "edited subject"}
        )
        refetched = await client.call_tool("get_message", {"message_id": message_id})

    assert _structured(result)["subject"] == "edited subject"
    assert _structured(refetched)["subject"] == "edited subject"
    # Body was left untouched -- only the field actually passed changes.
    assert _structured(refetched)["body"] == "Hi Jane, ..."


async def test_add_suppression_tool_against_real_service() -> None:
    service = InMemoryPlatformService()
    server = build_server(service)

    async with create_connected_server_and_client_session(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "add_suppression", {"address": "bounced@example.com", "scope": "ADDRESS"}
        )

    data = _structured(result)
    assert data["address"] == "bounced@example.com"
    assert data["scope"] == "ADDRESS"
    assert "bounced@example.com" in service._suppressed


async def test_pause_campaign_confirmation_gate_against_real_service() -> None:
    service = InMemoryPlatformService()
    server = build_server(service)
    campaign_id = next(iter(service._campaigns))

    async with create_connected_server_and_client_session(server, raise_exceptions=True) as client:
        preview = await client.call_tool("pause_campaign", {"campaign_id": campaign_id})
        confirmed = await client.call_tool(
            "pause_campaign", {"campaign_id": campaign_id, "confirm": True}
        )

    assert _structured(preview)["confirmed"] is False
    assert _structured(preview)["campaign"] is None

    confirmed_data = _structured(confirmed)
    assert confirmed_data["confirmed"] is True
    assert confirmed_data["campaign"]["state"] == "PAUSED"


async def test_report_resource_against_real_service() -> None:
    service = InMemoryPlatformService()
    server = build_server(service)
    campaign_id = next(iter(service._campaigns))

    async with create_connected_server_and_client_session(server, raise_exceptions=True) as client:
        result = await client.read_resource(AnyUrl(f"campaign://{campaign_id}/report"))

    payload = json.loads(result.contents[0].text)  # type: ignore[union-attr]
    assert payload["campaign_id"] == campaign_id
    assert "sources" in payload
    assert len(payload["sources"]) > 0


def test_main_wires_run_stdio_to_a_real_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """__main__.main() is the literal command in the README's config block.
    test_server_starts_over_stdio already proves it works end to end as a
    real subprocess (uncounted by this process's coverage); this exercises
    the same wiring in-process: it calls anyio.run(run_stdio, <a real
    InMemoryPlatformService>), not a placeholder.
    """
    import app.mcp.server.__main__ as entry_point

    captured_func: list[object] = []
    captured_service: list[object] = []

    def fake_run(func: object, service: object) -> None:
        captured_func.append(func)
        captured_service.append(service)

    monkeypatch.setattr("anyio.run", fake_run)
    entry_point.main()

    assert captured_func == [run_stdio]
    assert len(captured_service) == 1
    assert isinstance(captured_service[0], InMemoryPlatformService)


async def test_query_leads_not_found_becomes_mcp_error() -> None:
    """The InMemoryPlatformService's not-found path (a real ValueError from
    a real service, not FakePlatformService's NotImplementedError) also
    converts to isError, not a raised exception.
    """
    service = InMemoryPlatformService()
    server = build_server(service)

    async with create_connected_server_and_client_session(server, raise_exceptions=False) as client:
        result = await client.call_tool("get_campaign", {"campaign_id": "does-not-exist"})

    assert result.isError is True
