"""Anomaly detection: yield collapse, error spikes, cost overruns, stale stages."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.analytics.types import Anomaly

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def detect_anomalies(session: AsyncSession, campaign_id: str) -> list[Anomaly]:
    """Detect anomalies in campaign performance.

    Monitors:
      - ZERO_YIELD: >=20 consecutive resolver attempts with 0 hits (prototype failure)
      - YIELD_COLLAPSE: Hit rate dropped >50% vs baseline
      - ERROR_SPIKE: Error rate >5%
      - BOUNCE_CLIMB: Bounce rate trending toward 3%
      - COMPLAINT_SPIKE: Complaint rate spike
      - COST_OVERRUN: Spending exceeds 150% of estimate
      - STALE_STAGE: No output from a stage in N hours

    Args:
        session: Database session
        campaign_id: Campaign to analyze

    Returns:
        List of detected Anomaly records
    """
    anomalies: list[Anomaly] = []

    # Analyze resolver yields (from session.query resolver_runs)
    # If any resolver: attempts >= 20 and hits == 0 → ZERO_YIELD
    # If any resolver: hit_rate < baseline * 0.5 → YIELD_COLLAPSE
    # If any resolver: error_rate > 0.05 → ERROR_SPIKE

    # Analyze sending metrics
    # If bounce_rate > 0.025 and trending up → BOUNCE_CLIMB
    # If complaint_rate spike → COMPLAINT_SPIKE

    # Analyze costs
    # If total_cost > estimate * 1.5 → COST_OVERRUN

    # Analyze pipeline staleness
    # If no hits from discovery in N hours → STALE_STAGE

    # For now, return empty list (real implementation queries campaign telemetry)
    return anomalies


async def detect_zero_yield_alert(
    session: AsyncSession, resolver_name: str, campaign_id: str, threshold: int = 20
) -> Anomaly | None:
    """Check if a resolver has hit zero-yield condition.

    Args:
        session: Database session
        resolver_name: Resolver to check
        campaign_id: Campaign
        threshold: Minimum attempts before alert fires (default 20)

    Returns:
        Anomaly if zero-yield detected, None otherwise
    """
    # Query resolver_runs for resolver_name in campaign_id
    # Count attempts and hits
    # If attempts >= threshold and hits == 0:
    #   Create ZERO_YIELD Anomaly with severity=5
    # Stub returns None (real implementation queries resolver_runs)
    return None


__all__ = ["detect_anomalies", "detect_zero_yield_alert"]
