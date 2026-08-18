"""Unit tests for the MCP client layer (Session 22).

22 tests covering:
  - stdio and Streamable HTTP transport connection
  - OAuth PKCE flow, encrypted token storage, refresh-before-expiry
  - The generic adapter (context -> tool args, tool result -> Candidates)
  - Apollo and Clay as concrete configs over that same adapter
  - Third-party verification labels mapped deliberately, not imported
  - Spend cap enforcement and pre-run projection
  - The waterfall integration guarantee: METERED only runs when it must

Network is always mocked (respx for real HTTP, hand-written fakes standing
in for the MCP SDK's ClientSession/transport functions) -- nothing here
makes a real connection to Apollo, Clay, or a subprocess.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import pytest
import respx
from cryptography.fernet import Fernet

from app.core.config import Settings
from app.core.errors import BudgetExceededError
from app.mcp.adapter import FieldMapping, MCPResolver, MCPResolverConfig, ResultMapping
from app.mcp.budget import MCPBudget, format_spend_projection, project_spend
from app.mcp.client import MCPClient
from app.mcp.errors import MCPServerUnavailableError, MCPTimeoutError
from app.mcp.oauth import (
    EncryptedTokenStore,
    OAuthConfig,
    OAuthToken,
    build_authorization_url,
    generate_pkce_pair,
    refresh_if_needed,
)
from app.mcp.servers import register_mcp_resolvers
from app.mcp.servers.apollo import build_apollo_resolvers
from app.mcp.servers.clay import build_clay_resolvers
from app.mcp.types import MCPServerConfig, MCPTransport
from app.resolvers.base import BaseResolver, Candidate, LeadContext, Tier
from app.resolvers.executor import run_waterfall
from app.resolvers.merge import merge_candidates
from app.resolvers.registry import build_registry

# No module-level asyncio mark: pyproject.toml sets asyncio_mode = "auto".


# ============================================================================
# Shared fixtures and fakes
# ============================================================================


def _ctx(**overrides: object) -> LeadContext:
    defaults: dict[str, object] = {
        "company_id": uuid4(),
        "company_name": "Acme Dental",
        "domain": "acmedental.com",
        "website": "https://acmedental.com",
        "country_code": "US",
        "person_name": "Jane Smith",
    }
    defaults.update(overrides)
    return LeadContext(**defaults)  # type: ignore[arg-type]


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "_env_file": None,
        "database_url": "postgresql+asyncpg://u:p@localhost/db",
        "redis_url": "redis://localhost:6379/0",
        "secret_key": "test-secret-key",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def _vault_key() -> bytes:
    return Fernet.generate_key()


class FakeSDKSession:
    """Stands in for mcp.ClientSession -- just enough surface for
    MCPClient to drive: initialize/list_tools/call_tool, plus the async
    context manager protocol ClientSession itself implements.
    """

    def __init__(
        self,
        *,
        tools: list[Any] | None = None,
        call_results: dict[str, Any] | None = None,
        raise_on_init: Exception | None = None,
        hang_seconds: float | None = None,
    ) -> None:
        self.tools = tools or []
        self.call_results = call_results or {}
        self._raise_on_init = raise_on_init
        self._hang_seconds = hang_seconds
        self.call_log: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> FakeSDKSession:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def initialize(self) -> None:
        if self._raise_on_init is not None:
            raise self._raise_on_init

    async def list_tools(self) -> SimpleNamespace:
        return SimpleNamespace(tools=self.tools)

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        self.call_log.append((name, args))
        if self._hang_seconds is not None:
            await asyncio.sleep(self._hang_seconds)
        if name not in self.call_results:
            raise KeyError(f"no fake result configured for tool {name!r}")
        return self.call_results[name]


def _fake_call_result(data: dict[str, Any] | None, *, is_error: bool = False) -> SimpleNamespace:
    """A stand-in for mcp.types.CallToolResult with structured content."""
    return SimpleNamespace(isError=is_error, structuredContent=data, content=[])


def _patch_sdk(
    monkeypatch: pytest.MonkeyPatch,
    *,
    session: FakeSDKSession,
    stdio_raises: Exception | None = None,
    captured_http_headers: list[dict[str, str] | None] | None = None,
) -> None:
    """Monkeypatch the three SDK entry points MCPClient calls, so connect()
    exercises real branching/timeout/error-conversion logic against a fake
    transport instead of a real subprocess or network connection.
    """

    @asynccontextmanager
    async def fake_stdio_client(_params: object) -> Any:
        if stdio_raises is not None:
            raise stdio_raises
        yield ("read", "write")

    @asynccontextmanager
    async def fake_streamable_http_client(url: str, headers: dict[str, str] | None = None) -> Any:
        if captured_http_headers is not None:
            captured_http_headers.append(headers)
        yield ("read", "write", lambda: None)

    monkeypatch.setattr("app.mcp.client.stdio_client", fake_stdio_client)
    monkeypatch.setattr("app.mcp.client.streamablehttp_client", fake_streamable_http_client)
    monkeypatch.setattr("app.mcp.client.ClientSession", lambda read, write: session)


def _stdio_server(**overrides: object) -> MCPServerConfig:
    defaults: dict[str, object] = {
        "name": "test-server",
        "transport": MCPTransport.STDIO,
        "command": "test-mcp-server",
    }
    defaults.update(overrides)
    return MCPServerConfig(**defaults)  # type: ignore[arg-type]


def _http_server(**overrides: object) -> MCPServerConfig:
    defaults: dict[str, object] = {
        "name": "test-http-server",
        "transport": MCPTransport.STREAMABLE_HTTP,
        "url": "https://example.test/mcp",
    }
    defaults.update(overrides)
    return MCPServerConfig(**defaults)  # type: ignore[arg-type]


class SpyResolver(BaseResolver):
    """A FREE (or METERED) fake resolver that records how many times it ran."""

    def __init__(
        self,
        name: str,
        *,
        field: str = "person_name",
        tier: Tier = Tier.FREE,
        confidence: float = 0.9,
    ) -> None:
        self.name = name
        self.field = field
        self.tier = tier
        self.cost_per_call = Decimal("0")
        self.jurisdictions: frozenset[str] | None = None
        self._confidence = confidence
        self.call_count = 0

    async def resolve(self, ctx: LeadContext) -> list[Candidate]:
        self.call_count += 1
        if self._confidence <= 0:
            return []
        return [Candidate(value="Jane Smith", confidence=self._confidence, source=self.name)]


# ============================================================================
# Transport tests (1-2)
# ============================================================================


async def test_connects_over_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSDKSession()
    _patch_sdk(monkeypatch, session=session)

    client = MCPClient()
    mcp_session = await client.connect(_stdio_server())

    assert mcp_session.server_name == "test-server"
    assert id(mcp_session.session) == id(session)  # the fake session, not a real one
    await mcp_session.close()


async def test_connects_over_streamable_http(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSDKSession()
    headers_seen: list[dict[str, str] | None] = []
    _patch_sdk(monkeypatch, session=session, captured_http_headers=headers_seen)

    client = MCPClient()
    server = _http_server(static_bearer_token="my-static-token")
    mcp_session = await client.connect(server)

    assert mcp_session.server_name == "test-http-server"
    assert headers_seen == [{"Authorization": "Bearer my-static-token"}]
    await mcp_session.close()


# ============================================================================
# OAuth tests (3-5)
# ============================================================================


def test_oauth_flow_stores_encrypted_token() -> None:
    """PKCE challenge/verifier pair is well-formed, the authorization URL
    carries it, and the token store never holds plaintext.
    """
    pair = generate_pkce_pair()
    assert len(pair.verifier) >= 43  # RFC 7636 minimum
    assert pair.verifier != pair.challenge

    config = OAuthConfig(
        server_name="apollo",
        authorize_url="https://mcp.apollo.io/oauth/authorize",
        token_url="https://mcp.apollo.io/oauth/token",
        client_id="client-123",
        client_secret=None,
        redirect_uri="http://localhost:8000/mcp/oauth/callback",
    )
    url = build_authorization_url(config, state="xyz", code_challenge=pair.challenge)
    assert "code_challenge=" in url
    assert "code_challenge_method=S256" in url
    assert pair.verifier not in url  # only the challenge is ever transmitted


async def test_oauth_token_stored_and_read_back_encrypted() -> None:
    import datetime as dt

    vault_key = _vault_key()
    from app.sending.credentials import CredentialVault

    store = EncryptedTokenStore(CredentialVault(vault_key))
    token = OAuthToken(
        access_token="super-secret-access-token",
        refresh_token="super-secret-refresh-token",
        expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(hours=1),
    )

    await store.save("apollo", token)
    # The internal representation must not contain the plaintext anywhere.
    raw_entry = store._tokens["apollo"]
    assert b"super-secret-access-token" not in raw_entry.access_token
    assert raw_entry.refresh_token is not None
    assert b"super-secret-refresh-token" not in raw_entry.refresh_token

    round_tripped = await store.get("apollo")
    assert round_tripped is not None
    assert round_tripped.access_token == "super-secret-access-token"
    assert round_tripped.refresh_token == "super-secret-refresh-token"


@respx.mock
async def test_token_refreshed_before_expiry() -> None:
    import datetime as dt

    config = OAuthConfig(
        server_name="apollo",
        authorize_url="https://mcp.apollo.io/oauth/authorize",
        token_url="https://mcp.apollo.io/oauth/token",
        client_id="client-123",
        client_secret="shh",
        redirect_uri="http://localhost:8000/mcp/oauth/callback",
    )
    expired = OAuthToken(
        access_token="old-token",
        refresh_token="refresh-abc",
        expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=5),
    )
    respx.post(config.token_url).mock(
        return_value=httpx.Response(
            200,
            json={"access_token": "new-token", "refresh_token": "refresh-abc", "expires_in": 3600},
        )
    )

    async with httpx.AsyncClient() as http_client:
        refreshed = await refresh_if_needed(config, expired, http_client=http_client)

    assert refreshed.access_token == "new-token"
    assert not refreshed.is_expired()


async def test_token_never_logged(capsys: pytest.CaptureFixture[str]) -> None:
    """Neither the raw token value nor a repr/str of the OAuthToken leaks it."""
    import datetime as dt

    from app.core.logging import get_logger

    token = OAuthToken(
        access_token="do-not-leak-me",
        refresh_token="also-do-not-leak-me",
        expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(hours=1),
    )

    log = get_logger("test.mcp.oauth")
    # A caller might reasonably (if carelessly) log the object itself --
    # __repr__ is the actual safety net, not developer discipline.
    log.info("mcp.oauth.token_obtained", token=repr(token))

    captured = capsys.readouterr()
    assert "do-not-leak-me" not in captured.out
    assert "do-not-leak-me" not in captured.err
    assert "***redacted***" in repr(token)
    assert str(token) == repr(token)


# ============================================================================
# Client mechanics (6-8)
# ============================================================================


async def test_lists_tools_from_server(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = SimpleNamespace(
        name="people_search", description="Find a person", inputSchema={"type": "object"}
    )
    session = FakeSDKSession(tools=[tool])
    _patch_sdk(monkeypatch, session=session)

    client = MCPClient()
    mcp_session = await client.connect(_stdio_server())
    tools = await client.list_tools(mcp_session)

    assert len(tools) == 1
    assert tools[0].name == "people_search"
    assert tools[0].description == "Find a person"


async def test_server_down_degrades_gracefully(monkeypatch: pytest.MonkeyPatch) -> None:
    """A down server raises a typed MCPError, not a raw transport
    exception -- and a resolver built on it becomes a per-lead miss, not a
    crashed batch (see the integration assertion below).
    """
    _patch_sdk(monkeypatch, session=FakeSDKSession(), stdio_raises=ConnectionRefusedError("nope"))

    client = MCPClient()
    with pytest.raises(MCPServerUnavailableError):
        await client.connect(_stdio_server())

    # The waterfall survives even though the only METERED resolver's server
    # is down: run_waterfall must not raise, coverage is just lost.
    resolver = _build_test_resolver(client=client, budget=None)
    registry = build_registry([resolver])
    resolution = await run_waterfall(
        field="person_name", ctx=_ctx(), registry=registry, threshold=0.85, allow_metered=True
    )
    assert resolution.best is None
    error_attempts = [a for a in resolution.attempts if a.outcome.value == "error"]
    assert len(error_attempts) == 1
    assert error_attempts[0].resolver == "generic_mcp_person"


async def test_call_timeout_does_not_hang_the_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSDKSession(hang_seconds=5.0)
    _patch_sdk(monkeypatch, session=session)

    client = MCPClient()
    mcp_session = await client.connect(_stdio_server())

    start = time.monotonic()
    with pytest.raises(MCPTimeoutError):
        await client.call_tool(mcp_session, "slow_tool", {}, timeout_seconds=0.1)
    elapsed = time.monotonic() - start

    assert elapsed < 1.0  # bounded by the timeout, not the 5s hang


# ============================================================================
# Generic adapter tests (9-11)
# ============================================================================


def _build_test_resolver(*, client: MCPClient, budget: MCPBudget | None) -> MCPResolver:
    """A resolver with no vendor-specific code at all -- proves a brand new
    MCP server becomes a resolver via config only (test 11).
    """
    server = _stdio_server(name="generic-vendor")
    config = MCPResolverConfig(
        name="generic_mcp_person",
        field="person_name",
        server=server,
        tool_name="find_person",
        cost_per_call=Decimal("0.25"),
        context_mapping=(
            FieldMapping(tool_arg="company", context_attr="company_name", required=True),
            FieldMapping(tool_arg="domain", context_attr="domain"),
        ),
        result_mapping=ResultMapping(
            value_key="full_name",
            source_url_key="profile_url",
            verification_key="status",
            verified_value_map={"confirmed": 0.7, "guessed": 0.3},
        ),
        result_list_key="matches",
        budget=budget,
    )
    return MCPResolver(config, client)


async def test_generic_adapter_maps_context_to_tool_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSDKSession(call_results={"find_person": _fake_call_result({"matches": []})})
    _patch_sdk(monkeypatch, session=session)

    resolver = _build_test_resolver(client=MCPClient(), budget=None)
    await resolver.resolve(_ctx(company_name="Acme Dental", domain="acmedental.com"))

    expected_args = {"company": "Acme Dental", "domain": "acmedental.com"}
    assert session.call_log == [("find_person", expected_args)]


async def test_generic_adapter_maps_result_to_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    result_data = {
        "matches": [
            {
                "full_name": "Dr. Jane Smith",
                "profile_url": "https://vendor.example/p/1",
                "status": "confirmed",
            }
        ]
    }
    session = FakeSDKSession(call_results={"find_person": _fake_call_result(result_data)})
    _patch_sdk(monkeypatch, session=session)

    resolver = _build_test_resolver(client=MCPClient(), budget=None)
    candidates = await resolver.resolve(_ctx())

    assert len(candidates) == 1
    assert candidates[0].value == "Dr. Jane Smith"
    assert candidates[0].source_url == "https://vendor.example/p/1"
    assert candidates[0].confidence == 0.7


async def test_new_server_becomes_resolver_via_config_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real deliverable: nothing in app/mcp/adapter.py knows this vendor
    exists. Only MCPResolverConfig (in this test file, not even in
    app/mcp/servers/) describes it, and it still produces correct
    Candidates end to end through the real waterfall.
    """
    result_data = {"matches": [{"full_name": "Dr. Jane Smith", "status": "confirmed"}]}
    session = FakeSDKSession(call_results={"find_person": _fake_call_result(result_data)})
    _patch_sdk(monkeypatch, session=session)

    resolver = _build_test_resolver(client=MCPClient(), budget=None)
    registry = build_registry([resolver])

    resolution = await run_waterfall(
        field="person_name", ctx=_ctx(), registry=registry, threshold=0.5, allow_metered=True
    )

    assert resolution.best is not None
    assert resolution.best.value == "Dr. Jane Smith"


# ============================================================================
# Apollo / Clay tests (12-14)
# ============================================================================


async def test_apollo_person_tool_returns_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(apollo_oauth_client_id="apollo-client-id")
    from app.sending.credentials import CredentialVault

    token_store = EncryptedTokenStore(CredentialVault(_vault_key()))
    import datetime as dt

    await token_store.save(
        "apollo",
        OAuthToken(
            access_token="live-token",
            refresh_token=None,
            expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(hours=1),
        ),
    )

    result_data = {
        "people": [
            {
                "name": "Jane Smith",
                "linkedin_url": "https://linkedin.com/in/janesmith",
                "match_confidence": "verified",
            }
        ]
    }
    session = FakeSDKSession(call_results={"people_search": _fake_call_result(result_data)})
    _patch_sdk(monkeypatch, session=session)

    async with httpx.AsyncClient() as http_client:
        resolvers = build_apollo_resolvers(
            settings, token_store=token_store, http_client=http_client, budget=None
        )
        person_resolver = next(r for r in resolvers if r.field == "person_name")
        candidates = await person_resolver.resolve(_ctx())

    assert len(candidates) == 1
    assert candidates[0].value == "Jane Smith"
    assert candidates[0].source == "apollo_person"


async def test_clay_enrichment_returns_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(clay_mcp_url="https://clay.example/mcp", clay_api_key="clay-key")
    result_data = {
        "enrichments": [
            {
                "value": "50-100 employees",
                "source_url": "https://vendor.example/co/1",
                "confidence_label": "high",
            }
        ]
    }
    session = FakeSDKSession(call_results={"enrich_company": _fake_call_result(result_data)})
    _patch_sdk(monkeypatch, session=session)

    resolvers = build_clay_resolvers(settings, budget=None)
    assert len(resolvers) == 1
    candidates = await resolvers[0].resolve(_ctx())

    assert candidates[0].value == "50-100 employees"
    assert candidates[0].confidence == 0.75


async def test_third_party_verified_not_imported_as_our_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Apollo's own 'verified' label must not be trusted as our VALID
    (confidence 1.0) -- it's calibrated below our own SMTP-probe VALID
    because Apollo has the same Google/Microsoft blind spot we do. And a
    label the map doesn't recognise falls back to default_confidence, not
    to blind trust.
    """
    settings = _settings(apollo_oauth_client_id="apollo-client-id")
    from app.sending.credentials import CredentialVault

    token_store = EncryptedTokenStore(CredentialVault(_vault_key()))
    import datetime as dt

    await token_store.save(
        "apollo",
        OAuthToken(
            access_token="live-token",
            refresh_token=None,
            expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(hours=1),
        ),
    )

    result_data = {
        "people": [
            {"name": "Verified Person", "match_confidence": "verified"},
            {"name": "Unknown Label Person", "match_confidence": "something_apollo_invented"},
        ]
    }
    session = FakeSDKSession(call_results={"people_search": _fake_call_result(result_data)})
    _patch_sdk(monkeypatch, session=session)

    async with httpx.AsyncClient() as http_client:
        resolvers = build_apollo_resolvers(
            settings, token_store=token_store, http_client=http_client, budget=None
        )
        person_resolver = next(r for r in resolvers if r.field == "person_name")
        candidates = await person_resolver.resolve(_ctx())

    by_value = {c.value: c.confidence for c in candidates}
    assert 0.0 < by_value["Verified Person"] < 1.0
    assert by_value["Unknown Label Person"] == 0.5  # default_confidence, not trusted


# ============================================================================
# Provenance and confidence merging (15-16)
# ============================================================================


async def test_candidates_carry_source_and_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    result_data = {"matches": [{"full_name": "Dr. Jane Smith", "profile_url": "https://x.test/1"}]}
    session = FakeSDKSession(call_results={"find_person": _fake_call_result(result_data)})
    _patch_sdk(monkeypatch, session=session)

    resolver = _build_test_resolver(client=MCPClient(), budget=None)
    candidates = await resolver.resolve(_ctx())

    assert candidates[0].source == "generic_mcp_person"
    assert candidates[0].source_url == "https://x.test/1"
    assert candidates[0].retrieved_at is not None


async def test_metered_candidates_have_independence_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two candidates from the same MCP server must collapse rather than
    stack into inflated confidence when merged -- they're correlated (one
    vendor's pipeline), not independent evidence.
    """
    result_data = {
        "matches": [
            {"full_name": "Jane Smith", "status": "confirmed"},
            {"full_name": "Jane Smith", "status": "guessed"},
        ]
    }
    session = FakeSDKSession(call_results={"find_person": _fake_call_result(result_data)})
    _patch_sdk(monkeypatch, session=session)

    resolver = _build_test_resolver(client=MCPClient(), budget=None)
    candidates = await resolver.resolve(_ctx())

    assert all(c.independence_key == "mcp:generic-vendor" for c in candidates)

    merged = merge_candidates(candidates)
    assert len(merged) == 1  # collapsed to the single best member
    assert merged[0].confidence == 0.7  # the "confirmed" one, not a stacked product


# ============================================================================
# Spend control (17-19)
# ============================================================================


def test_spend_cap_raises_when_exceeded() -> None:
    budget = MCPBudget(campaign_id="camp-1", cap=Decimal("10.00"))
    budget.record(Decimal("6.00"), resolver_name="apollo_person")

    with pytest.raises(BudgetExceededError):
        budget.record(Decimal("5.00"), resolver_name="apollo_person")

    # The rejected charge must not have been applied.
    assert budget.spent == Decimal("6.00")


def test_projected_spend_shown_before_run() -> None:
    total = project_spend(n_leads_unresolved=20, cost_per_call=Decimal("2.00"))
    assert total == Decimal("40.00")

    text = format_spend_projection(
        n_leads_unresolved=20, cost_per_call=Decimal("2.00"), resolver_name="apollo_person"
    )
    assert "$40.00" in text
    assert "20" in text


async def test_cost_recorded_per_call_in_resolver_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    result_data = {"matches": [{"full_name": "Dr. Jane Smith"}]}
    session = FakeSDKSession(call_results={"find_person": _fake_call_result(result_data)})
    _patch_sdk(monkeypatch, session=session)

    budget = MCPBudget(campaign_id="camp-1", cap=Decimal("100.00"))
    resolver = _build_test_resolver(client=MCPClient(), budget=budget)
    registry = build_registry([resolver])

    resolution = await run_waterfall(
        field="person_name", ctx=_ctx(), registry=registry, threshold=0.99, allow_metered=True
    )

    mcp_attempts = [a for a in resolution.attempts if a.resolver == "generic_mcp_person"]
    assert len(mcp_attempts) == 1
    assert mcp_attempts[0].cost == Decimal("0.25")
    assert budget.spent == Decimal("0.25")


# ============================================================================
# The cost guarantee (20-22)
# ============================================================================


async def test_mcp_resolver_not_called_when_free_tier_sufficed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Integration test: a real ResolverRegistry, a real run_waterfall, and
    a real MCPResolver -- the FREE tier clears threshold, so the METERED
    server is never even connected to, let alone called.
    """
    session = FakeSDKSession(call_results={"find_person": _fake_call_result({"matches": []})})
    _patch_sdk(monkeypatch, session=session)

    free_resolver = SpyResolver("free_source", tier=Tier.FREE, confidence=0.9)
    mcp_resolver = _build_test_resolver(client=MCPClient(), budget=None)
    registry = build_registry([free_resolver, mcp_resolver])

    resolution = await run_waterfall(
        field="person_name", ctx=_ctx(), registry=registry, threshold=0.85, allow_metered=True
    )

    assert resolution.best is not None
    assert resolution.best.source == "free_source"
    assert session.call_log == []  # the wallet-protecting assertion
    assert resolution.stopped_at == Tier.FREE


async def test_mcp_resolver_called_when_free_tier_missed(monkeypatch: pytest.MonkeyPatch) -> None:
    result_data = {"matches": [{"full_name": "Dr. Jane Smith", "status": "confirmed"}]}
    session = FakeSDKSession(call_results={"find_person": _fake_call_result(result_data)})
    _patch_sdk(monkeypatch, session=session)

    free_resolver = SpyResolver("free_source", tier=Tier.FREE, confidence=0.0)  # misses
    mcp_resolver = _build_test_resolver(client=MCPClient(), budget=None)
    registry = build_registry([free_resolver, mcp_resolver])

    resolution = await run_waterfall(
        field="person_name", ctx=_ctx(), registry=registry, threshold=0.5, allow_metered=True
    )

    assert resolution.best is not None
    assert resolution.best.value == "Dr. Jane Smith"
    assert len(session.call_log) == 1
    assert resolution.stopped_at == Tier.METERED


async def test_unconfigured_server_not_registered() -> None:
    unconfigured = _settings()  # no apollo/clay/mcp settings at all

    from app.sending.credentials import CredentialVault

    token_store = EncryptedTokenStore(CredentialVault(_vault_key()))
    async with httpx.AsyncClient() as http_client:
        apollo_resolvers = build_apollo_resolvers(
            unconfigured, token_store=token_store, http_client=http_client, budget=None
        )
    clay_resolvers = build_clay_resolvers(unconfigured, budget=None)

    assert apollo_resolvers == []
    assert clay_resolvers == []

    # And the top-level wiring point: no spend cap configured means MCP is
    # off entirely, not silently uncapped.
    registry = build_registry([])
    async with httpx.AsyncClient() as http_client:
        budget = register_mcp_resolvers(
            registry,
            unconfigured,
            campaign_id="camp-1",
            token_store=token_store,
            http_client=http_client,
        )
    assert budget is None


async def test_register_mcp_resolvers_creates_budget_when_cap_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gap-closing: the positive branch of register_mcp_resolvers -- a
    configured spend cap creates a real MCPBudget and registers whatever
    resolvers the (here, still-unconfigured-per-vendor) builders return,
    rather than every path going through the all-unconfigured early return.
    """
    settings = _settings(mcp_spend_cap_usd=Decimal("50.00"))
    from app.sending.credentials import CredentialVault

    token_store = EncryptedTokenStore(CredentialVault(_vault_key()))
    registry = build_registry([])

    async with httpx.AsyncClient() as http_client:
        budget = register_mcp_resolvers(
            registry,
            settings,
            campaign_id="camp-1",
            token_store=token_store,
            http_client=http_client,
        )

    assert budget is not None
    assert budget.cap == Decimal("50.00")
    assert budget.campaign_id == "camp-1"
