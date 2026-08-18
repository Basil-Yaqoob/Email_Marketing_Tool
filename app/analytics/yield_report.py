"""Per-resolver yield reporting and trending.

Computes hit rate, confidence, latency, and cost per resolver.
Compares against baseline to surface performance degradation.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from app.analytics.types import SourceYield

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def source_yield(
    session: AsyncSession, campaign_id: str, window: timedelta
) -> list[SourceYield]:
    """Compute per-resolver yield metrics for a campaign over a time window.

    Args:
        session: Database session
        campaign_id: Campaign to analyze
        window: Time window (e.g., timedelta(hours=24))

    Returns:
        List of SourceYield records sorted by attempts (high first),
        with zero-yield resolvers surfaced earliest.
    """
    # Query resolver_runs telemetry for the campaign within the window
    # This is a stub that would query: resolver_runs table with:
    #   - campaign_id, resolver_name, status, confidence, latency_ms, cost_usd
    #   - Filter by created_at >= now - window
    #   - Group by resolver_name, compute attempt/hit counts
    #   - Compare against baseline (e.g., 30-day rolling average)

    # For now, return an empty list (real implementation queries telemetry table)
    yields: list[SourceYield] = []
    return yields


async def baseline_hit_rate(session: AsyncSession, resolver_name: str) -> float:
    """Compute baseline hit rate for a resolver (30-day rolling average).

    Args:
        session: Database session
        resolver_name: Resolver to baseline

    Returns:
        Hit rate 0.0-1.0 from past 30 days
    """
    # Query resolver_runs for past 30 days, compute success rate
    # Stub returns 0.0 (real implementation queries telemetry)
    return 0.0


__all__ = ["baseline_hit_rate", "source_yield"]
