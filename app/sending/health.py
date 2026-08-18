"""Health score calculation for mailbox rotation and auto-pause.

A mailbox's health determines sending priority and whether it should be
temporarily paused. Bounce rate is weighted hardest since it is the fastest
route to a blocked domain.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.mailbox import Mailbox


async def health_score(
    mailbox: Mailbox,
    session: AsyncSession,
    window: timedelta = timedelta(days=7),
) -> float:
    """Calculate health score (0.0-1.0) for a mailbox.

    Inputs (when Send/Reply models are fully built in Sessions 16-18):
    - bounce_rate: Proportion of sends that bounced (hardest weight)
    - complaint_rate: Proportion that drew complaints (hard weight)
    - reply_rate: Proportion that got replies (positive signal)
    - dns_status: Whether DNS checks passed (blocks sending if failed)
    - warmup_stage: Whether still in warmup (capped volume)
    - age: How long the mailbox has been configured (newer = lower score)
    - recent_send_volume: Whether approaching daily cap (headroom)

    Returns:
        A float from 0.0 (unhealthy) to 1.0 (perfect). For now, returns a
        baseline score based on warmup stage and age until Send/Reply models
        are extended with bounce/complaint/received_at tracking.
    """
    # Stub implementation: only use data available in current schema.
    # Full implementation awaits Send.bounced_at/complained_at (Session 16)
    # and Reply.mailbox_id (Session 17).
    score = 1.0

    # Warmup penalty: new mailboxes should have lower health
    if mailbox.warmup_stage < 5:  # Still ramping up
        score -= 0.2

    # Age penalty: younger mailboxes are less trusted
    age = datetime.now(UTC) - mailbox.created_at
    if age < timedelta(days=7):
        score -= 0.1

    return max(0.0, min(1.0, score))


__all__ = ["health_score"]
