"""MCP resources -- read-only context a model benefits from without
having to ask a tool for it explicitly.

docs://limits is the one worth calling out: it's this platform's own hard
limits (CLAUDE.md section 10), verbatim. A client that has read it reports
"coverage tops out around 55-70%" instead of implying completeness, and
explains an UNKNOWN verification instead of calling it broken.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from app.mcp.server.service import PlatformService

HARD_LIMITS_TEXT = """\
Hard limits of this platform (see CLAUDE.md section 10). State these \
plainly; never imply the product does better than this.

- Decision-maker email coverage tops out around 55-70%, not 100%.
- SMTP verification is blind against Google Workspace and Microsoft 365 \
hosted domains, and against catch-all domains. Those resolve to UNKNOWN \
and are handled by sending policy (a lower-volume track), not by better \
code. UNKNOWN is a normal, expected outcome -- not a failure and not a \
sign anything is broken.
- Sending infrastructure costs money (roughly $175-315/mo). BYO API keys \
makes the intelligence gathering nearly free; it cannot make mailboxes \
free.
- No safe automated LinkedIn scraping exists in this platform. It uses \
SERP snippets (site:linkedin.com/in searches) which never request a page \
from linkedin.com directly, and it never will build cookie-based \
LinkedIn scraping.
- Transactional email providers (SendGrid, Mailgun, Postmark, SES) ban \
cold outreach in their terms of service. This platform never integrates \
them as a sending path.
"""


def register_resources(mcp: FastMCP, service: PlatformService) -> None:
    """Register every resource against `service`. Mirrors
    app.mcp.server.tools.register_tools's shape and call site.
    """

    @mcp.resource(
        "campaign://{campaign_id}/funnel",
        name="campaign_funnel",
        description="Current funnel counts for one campaign (discovered, with_website, "
        "person_found, email_found, verified, sent, replied), refreshed live.",
        mime_type="application/json",
    )
    async def campaign_funnel(campaign_id: str) -> dict[str, int]:
        return await service.get_funnel(campaign_id)

    @mcp.resource(
        "campaign://{campaign_id}/report",
        name="campaign_report",
        description="The full Session 19 analytics report for one campaign: funnel, "
        "reply rate, per-source yield, and inbox placement by provider.",
        mime_type="application/json",
    )
    async def campaign_report(campaign_id: str) -> dict[str, object]:
        return await service.get_report(campaign_id)

    @mcp.resource(
        "docs://limits",
        name="hard_limits",
        description="This platform's documented hard limits -- read this before telling "
        "a user what the platform can do, so claims stay honest about coverage, "
        "UNKNOWN verification, and what it will never do.",
        mime_type="text/plain",
    )
    async def docs_limits() -> str:
        return HARD_LIMITS_TEXT


__all__ = ["HARD_LIMITS_TEXT", "register_resources"]
