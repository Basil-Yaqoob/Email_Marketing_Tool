"""Apollo (mcp.apollo.io/mcp) as METERED resolvers.

Thin config over app.mcp.adapter's generic mapping -- this file has no
Apollo-specific request/response handling code, only the data that
describes how Apollo's tools map onto our fields. Two resolvers ship here:
person lookup and email enrichment, both against the same server connection.

Apollo authenticates via OAuth (Authorization Code + PKCE, app.mcp.oauth).
The interactive authorization step (sending the user to Apollo's consent
screen, handling the redirect) happens once, out of band -- see
doc/MCP-SETUP.md. This module only consumes whatever token the flow already
stored; if none exists yet, the resolver degrades to unconfigured rather
than trying to drive a browser from a batch job.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

import httpx

from app.core.config import Settings
from app.core.logging import get_logger
from app.mcp.adapter import FieldMapping, MCPResolver, MCPResolverConfig, ResultMapping
from app.mcp.budget import MCPBudget
from app.mcp.client import AuthHeaderProvider, MCPClient
from app.mcp.errors import MCPServerUnavailableError
from app.mcp.oauth import OAuthConfig, TokenStore, refresh_if_needed
from app.mcp.types import MCPServerConfig, MCPTransport

log = get_logger(__name__)

SERVER_NAME = "apollo"

# Apollo publishes its own match-confidence label per person; we do not
# import their "verified" as our VALID. Calibrated deliberately, not
# assumed -- see the session's own note that Apollo has the same
# Google/Microsoft-hosted blind spot our SMTP prober does, so even their
# strongest label tops out below what our own rung-3 probe would earn.
_PERSON_VERIFIED_MAP: Mapping[str, float] = {
    "verified": 0.75,
    "likely": 0.5,
    "guessed": 0.3,
}
_EMAIL_VERIFIED_MAP: Mapping[str, float] = {
    "verified": 0.8,
    "likely": 0.45,
    "unverified": 0.25,
}


def _oauth_config(settings: Settings, *, redirect_uri: str) -> OAuthConfig | None:
    if settings.apollo_oauth_client_id is None:
        return None
    return OAuthConfig(
        server_name=SERVER_NAME,
        authorize_url="https://mcp.apollo.io/oauth/authorize",
        token_url="https://mcp.apollo.io/oauth/token",
        client_id=settings.apollo_oauth_client_id.get_secret_value(),
        client_secret=(
            settings.apollo_oauth_client_secret.get_secret_value()
            if settings.apollo_oauth_client_secret
            else None
        ),
        redirect_uri=redirect_uri,
        scope="mcp:tools",
    )


def _make_auth_header_provider(
    oauth_config: OAuthConfig, token_store: TokenStore, http_client: httpx.AsyncClient
) -> AuthHeaderProvider:
    async def provider(_server: MCPServerConfig) -> Mapping[str, str] | None:
        token = await token_store.get(oauth_config.server_name)
        if token is None:
            # Degrade, don't crash: this call fails with a clear reason,
            # the executor records it as a MISS-shaped ERROR, the run
            # continues on every other resolver.
            raise MCPServerUnavailableError(
                oauth_config.server_name,
                "no OAuth token on file -- run the Apollo authorization flow first "
                "(doc/MCP-SETUP.md)",
            )
        token = await refresh_if_needed(oauth_config, token, http_client=http_client)
        await token_store.save(oauth_config.server_name, token)
        return {"Authorization": f"{token.token_type} {token.access_token}"}

    return provider


def build_apollo_resolvers(
    settings: Settings,
    *,
    token_store: TokenStore,
    http_client: httpx.AsyncClient,
    budget: MCPBudget | None,
    redirect_uri: str = "http://localhost:8000/mcp/oauth/callback",
) -> list[MCPResolver]:
    """Build Apollo's resolvers, or [] if Apollo isn't configured at all.

    "Configured" means an OAuth client id is set -- without one there is no
    way to even start the authorization flow, so registering the resolver
    would just guarantee every call fails. A configured client id with no
    token *yet* still registers: the resolver degrades per-call (see
    _make_auth_header_provider) rather than being silently absent, which
    would look identical to "the free tier didn't need it".
    """
    oauth_config = _oauth_config(settings, redirect_uri=redirect_uri)
    if oauth_config is None:
        log.info("mcp.server.unconfigured", server=SERVER_NAME, reason="no oauth client id")
        return []

    auth_provider = _make_auth_header_provider(oauth_config, token_store, http_client)
    client = MCPClient(auth_header_provider=auth_provider)
    server = MCPServerConfig(
        name=SERVER_NAME,
        transport=MCPTransport.STREAMABLE_HTTP,
        url=settings.apollo_mcp_url,
    )
    cost_per_call = Decimal("1.00")

    person_resolver = MCPResolver(
        MCPResolverConfig(
            name="apollo_person",
            field="person_name",
            server=server,
            tool_name="people_search",
            cost_per_call=cost_per_call,
            context_mapping=(
                FieldMapping(
                    tool_arg="organization_name", context_attr="company_name", required=True
                ),
                FieldMapping(tool_arg="domain", context_attr="domain"),
            ),
            result_mapping=ResultMapping(
                value_key="name",
                source_url_key="linkedin_url",
                verification_key="match_confidence",
                verified_value_map=_PERSON_VERIFIED_MAP,
            ),
            result_list_key="people",
            budget=budget,
        ),
        client,
    )

    email_resolver = MCPResolver(
        MCPResolverConfig(
            name="apollo_email",
            field="email",
            server=server,
            tool_name="email_enrichment",
            cost_per_call=cost_per_call,
            context_mapping=(
                FieldMapping(tool_arg="full_name", context_attr="person_name", required=True),
                FieldMapping(tool_arg="domain", context_attr="domain", required=True),
            ),
            result_mapping=ResultMapping(
                value_key="email",
                verification_key="email_status",
                verified_value_map=_EMAIL_VERIFIED_MAP,
            ),
            budget=budget,
        ),
        client,
    )

    return [person_resolver, email_resolver]


__all__ = ["SERVER_NAME", "build_apollo_resolvers"]
