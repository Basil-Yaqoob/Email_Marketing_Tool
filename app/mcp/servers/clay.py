"""Clay's hosted MCP server (150+ data providers behind one enrichment
tool) as a METERED resolver.

Simpler than Apollo: Clay authenticates with a static API key rather than
OAuth, so there is no token-refresh lifecycle here -- just a config and a
result mapping, same pattern as apollo.py.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from app.core.config import Settings
from app.core.logging import get_logger
from app.mcp.adapter import FieldMapping, MCPResolver, MCPResolverConfig, ResultMapping
from app.mcp.budget import MCPBudget
from app.mcp.client import MCPClient
from app.mcp.types import MCPServerConfig, MCPTransport

log = get_logger(__name__)

SERVER_NAME = "clay"

# Clay aggregates other providers and passes through whichever source found
# the value, tagged with that source's own confidence label. We do not
# import it as our VALID -- see apollo.py's identical note.
_ENRICHMENT_VERIFIED_MAP: Mapping[str, float] = {
    "high": 0.75,
    "medium": 0.5,
    "low": 0.3,
}


def build_clay_resolvers(
    settings: Settings,
    *,
    budget: MCPBudget | None,
) -> list[MCPResolver]:
    """Build Clay's resolver, or [] if Clay isn't configured.

    "Configured" means both a server URL and an API key are set -- Clay's
    hosted server is provisioned per-workspace, so there's no shared
    default URL the way Apollo publishes one.
    """
    if settings.clay_mcp_url is None or settings.clay_api_key is None:
        log.info("mcp.server.unconfigured", server=SERVER_NAME, reason="no url or api key")
        return []

    client = MCPClient()
    server = MCPServerConfig(
        name=SERVER_NAME,
        transport=MCPTransport.STREAMABLE_HTTP,
        url=settings.clay_mcp_url,
        static_bearer_token=settings.clay_api_key.get_secret_value(),
    )

    enrichment_resolver = MCPResolver(
        MCPResolverConfig(
            name="clay_company_enrichment",
            field="company_info",
            server=server,
            tool_name="enrich_company",
            cost_per_call=Decimal("0.50"),
            context_mapping=(
                FieldMapping(tool_arg="domain", context_attr="domain", required=True),
            ),
            result_mapping=ResultMapping(
                value_key="value",
                source_url_key="source_url",
                verification_key="confidence_label",
                verified_value_map=_ENRICHMENT_VERIFIED_MAP,
            ),
            result_list_key="enrichments",
            budget=budget,
        ),
        client,
    )

    return [enrichment_resolver]


__all__ = ["SERVER_NAME", "build_clay_resolvers"]
