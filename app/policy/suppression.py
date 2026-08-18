"""Suppression list management — address, domain, and global scope.

Suppression is global by design: an unsubscribe in one campaign must suppress
across every campaign, or the product re-emails someone who explicitly opted out.

Checked twice: at message build time and again immediately before the SMTP
handshake, so a mid-campaign unsubscribe takes effect on the very next send.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.suppression import Suppression
from app.policy.types import SuppressionHit, SuppressionScope


async def is_suppressed(address: str, session: AsyncSession) -> SuppressionHit | None:
    """Check if an address is suppressed at address, domain, or global scope.

    Args:
        address: Email address to check (e.g. jane@acme.com)
        session: Database session

    Returns:
        SuppressionHit if suppressed, None otherwise.
    """
    # Extract domain from address
    if "@" not in address:
        return None

    _, domain = address.rsplit("@", 1)

    # Check in order: address, domain, global
    for check_value, scope in [
        (address.lower(), SuppressionScope.ADDRESS),
        (domain.lower(), SuppressionScope.DOMAIN),
        ("*", SuppressionScope.GLOBAL),
    ]:
        row = await session.execute(select(Suppression).where(Suppression.value == check_value))
        suppression = row.scalar_one_or_none()
        if suppression:
            return SuppressionHit(
                value=suppression.value,
                scope=scope,
                reason=suppression.reason,
                added_at=suppression.added_at.isoformat(),
            )

    return None


async def add_suppression(
    value: str,
    reason: str,
    session: AsyncSession,
) -> Suppression:
    """Add an address or domain to the suppression list.

    Args:
        value: Email address or domain to suppress (e.g. jane@acme.com or acme.com)
        reason: Why it's suppressed (e.g. "unsubscribe_click", "hard_bounce")
        session: Database session

    Returns:
        The created Suppression row.
    """
    # Normalize value
    normalized = value.lower()

    # Check if already exists (upsert logic: if it exists, return it)
    existing = await session.execute(select(Suppression).where(Suppression.value == normalized))
    suppression = existing.scalar_one_or_none()

    if suppression:
        return suppression

    # Create new suppression
    suppression = Suppression(
        value=normalized,
        reason=reason,
        added_at=datetime.now(UTC),
    )
    session.add(suppression)
    await session.flush()
    return suppression


__all__ = ["SuppressionHit", "SuppressionScope", "add_suppression", "is_suppressed"]
