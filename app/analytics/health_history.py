"""Mailbox health tracking over time.

Records snapshots of health scores and contributing factors so degradation
can be understood (bounce rate climb, complaint rate spike, etc).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from app.analytics.types import HealthSnapshot

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def record_health_snapshot(
    session: AsyncSession,
    mailbox_id: str,
    score: float,
    bounce_rate: float,
    complaint_rate: float,
    reply_rate: float,
    warmup_stage: int,
) -> HealthSnapshot:
    """Record a mailbox health snapshot.

    Args:
        session: Database session
        mailbox_id: Mailbox being measured
        score: Overall health score (0.0-1.0)
        bounce_rate: Hard+soft bounces as fraction
        complaint_rate: Complaints as fraction
        reply_rate: Positive replies as fraction
        warmup_stage: Current warmup stage (0-6)

    Returns:
        Recorded HealthSnapshot
    """
    # Insert into health_snapshots table
    # Stub returns the snapshot without persisting (real implementation saves to DB)
    return HealthSnapshot(
        mailbox_id=mailbox_id,
        score=score,
        bounce_rate=bounce_rate,
        complaint_rate=complaint_rate,
        reply_rate=reply_rate,
        warmup_stage=warmup_stage,
        measured_at=datetime.now(UTC),
    )


async def health_history(
    session: AsyncSession, mailbox_id: str, window: timedelta = timedelta(days=7)
) -> list[HealthSnapshot]:
    """Get health history for a mailbox over a time window.

    Args:
        session: Database session
        mailbox_id: Mailbox to query
        window: Time window (default 7 days)

    Returns:
        List of HealthSnapshot records ordered by measured_at (old→new)
    """
    # Query health_snapshots for mailbox_id where measured_at >= now - window
    # Order by measured_at ASC
    # Stub returns empty list (real implementation queries DB)
    return []


async def detect_health_decline(
    session: AsyncSession, mailbox_id: str, threshold: float = 0.15
) -> bool:
    """Detect if health score has declined significantly in the past 7 days.

    Args:
        session: Database session
        mailbox_id: Mailbox to check
        threshold: Minimum decline to trigger alert (default 0.15 = 15%)

    Returns:
        True if health declined by threshold or more
    """
    # Query past 7 days of snapshots, compare oldest to newest
    # Stub returns False (real implementation computes decline)
    return False


__all__ = ["detect_health_decline", "health_history", "record_health_snapshot"]
