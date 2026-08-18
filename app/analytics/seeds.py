"""Seed account placement testing.

Tests email deliverability by sending messages to known good seed accounts
and monitoring their folder placement (inbox, promotions, spam, missing).
Tracks changes mid-campaign to trigger alerts.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from app.analytics.types import Placement, SeedPlacement

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# Seed account definitions
SEED_ACCOUNTS = {
    "gmail": [
        "test-seed-001@gmail.com",
        "test-seed-002@gmail.com",
    ],
    "outlook": [
        "test-seed-001@outlook.com",
        "test-seed-002@outlook.com",
    ],
    "yahoo": [
        "test-seed-001@yahoo.com",
    ],
    "corporate": [
        "test-seed-001@company.local",
    ],
}


async def check_seed_placement(session: AsyncSession, seed_email: str, provider: str) -> Placement:
    """Check where a seed email landed (inbox, promotions, spam, missing).

    Args:
        session: Database session
        seed_email: The seed email address
        provider: Provider (gmail, outlook, yahoo, corporate)

    Returns:
        Placement enum indicating folder location
    """
    # Query IMAP for the seed mailbox, check folder labels
    # Gmail: look for label "INBOX" vs "[Gmail]/Promotions" vs "[Gmail]/Spam"
    # Outlook: look for folder paths
    # Stub returns MISSING (real implementation connects to seed mailboxes)
    return Placement.MISSING


async def test_all_seeds(session: AsyncSession, campaign_id: str) -> list[SeedPlacement]:
    """Run seed placement tests for all providers.

    Args:
        session: Database session
        campaign_id: Campaign being tested

    Returns:
        List of SeedPlacement results
    """
    results: list[SeedPlacement] = []

    for provider, accounts in SEED_ACCOUNTS.items():
        for seed_email in accounts:
            placement = await check_seed_placement(session, seed_email, provider)
            results.append(
                SeedPlacement(
                    seed_email=seed_email,
                    provider=provider,
                    placement=placement,
                    checked_at=datetime.now(UTC),
                )
            )

    return results


async def compare_placement_history(
    session: AsyncSession, campaign_id: str
) -> dict[str, list[Placement]]:
    """Compare seed placement over time to detect mid-campaign placement drops.

    Args:
        session: Database session
        campaign_id: Campaign to analyze

    Returns:
        Dict mapping seed_email to list of placements over time
    """
    # Query seed_placements table for campaign_id, group by seed_email, order by checked_at
    # Stub returns empty dict (real implementation queries placement history)
    return {}


__all__ = ["SEED_ACCOUNTS", "check_seed_placement", "compare_placement_history", "test_all_seeds"]
