"""Campaign funnel and reporting.

Computes end-to-end conversion funnel and generates performance reports.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.analytics.types import CampaignFunnel

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def campaign_funnel(session: AsyncSession, campaign_id: str) -> CampaignFunnel:
    """Compute campaign funnel matching doc/00-OVERVIEW.md shape.

    Args:
        session: Database session
        campaign_id: Campaign to analyze

    Returns:
        CampaignFunnel with counts at each stage
    """
    # Query counts at each stage:
    #   - discovered: leads table count for campaign_id
    #   - with_website: leads where website_url is not null
    #   - person_found: persons table where lead_id in campaign_id
    #   - email_found: persons where email is not null
    #   - verified: persons where email_verified = true
    #   - sent: messages where campaign_id and status = 'sent'
    #   - replied: messages where campaign_id and status in ('replied', 'interested')

    # Stub returns zeros (real implementation queries actual funnel)
    return CampaignFunnel(
        campaign_id=campaign_id,
        discovered=0,
        with_website=0,
        person_found=0,
        email_found=0,
        verified=0,
        sent=0,
        replied=0,
    )


async def conversion_rate(session: AsyncSession, campaign_id: str) -> float:
    """Compute overall conversion rate (replied / sent).

    Args:
        session: Database session
        campaign_id: Campaign to analyze

    Returns:
        Conversion rate as fraction (0.0-1.0)
    """
    # Query sent and replied counts
    # Stub returns 0.0 (real implementation computes actual rate)
    return 0.0


async def per_angle_conversion(
    session: AsyncSession, campaign_id: str
) -> dict[str, dict[str, int | float]]:
    """Compute conversion rate per angle (news_hook, review_quote, competitor).

    Args:
        session: Database session
        campaign_id: Campaign to analyze

    Returns:
        Dict mapping angle name to dict of {sent, replied, rate}
    """
    # Query messages grouped by angle, compute rate per angle
    # Stub returns empty dict (real implementation queries angle-tagged messages)
    return {}


__all__ = ["campaign_funnel", "conversion_rate", "per_angle_conversion"]
