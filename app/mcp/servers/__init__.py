"""Registers every configured MCP resolver into a ResolverRegistry.

The one place that knows about all MCP vendors -- apollo.py and clay.py are
the first two; a third vendor's module plugs in here alongside its own
config-only build_*_resolvers function, no other code changes needed.
"""

from __future__ import annotations

import httpx

from app.core.config import Settings
from app.core.logging import get_logger
from app.mcp.budget import MCPBudget
from app.mcp.oauth import TokenStore
from app.mcp.servers.apollo import build_apollo_resolvers
from app.mcp.servers.clay import build_clay_resolvers
from app.resolvers.registry import ResolverRegistry

log = get_logger(__name__)


def register_mcp_resolvers(
    registry: ResolverRegistry,
    settings: Settings,
    *,
    campaign_id: str,
    token_store: TokenStore,
    http_client: httpx.AsyncClient,
) -> MCPBudget | None:
    """Register every MCP resolver whose vendor is configured.

    Returns the shared MCPBudget, or None if `mcp_spend_cap_usd` isn't set.
    CLAUDE.md rule 2.3 ("free before paid, always") reads as "no cap
    configured" meaning MCP resolvers are never registered at all, not
    registered with an implicit uncapped spend -- the same reasoning
    app.core.config.Settings.llm_spend_cap_usd's docstring already applies
    to LLM spend.
    """
    if settings.mcp_spend_cap_usd is None:
        log.info("mcp.disabled", reason="no mcp_spend_cap_usd configured")
        return None

    budget = MCPBudget(campaign_id=campaign_id, cap=settings.mcp_spend_cap_usd)

    resolvers = [
        *build_apollo_resolvers(
            settings, token_store=token_store, http_client=http_client, budget=budget
        ),
        *build_clay_resolvers(settings, budget=budget),
    ]
    for resolver in resolvers:
        registry.register(resolver)

    return budget


__all__ = ["register_mcp_resolvers"]
