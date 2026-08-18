"""Per-company contact throttling.

Never contact more than N people at one company within a window
(default 1 per 14 days). Four emails from four addresses to four people
at the same 12-person business in one week is the fastest way to get
domain-blocked.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.message import Message
from app.db.models.person import Person
from app.db.models.send import Send


async def check_company_throttle(
    company_id: str,
    session: AsyncSession,
    max_contacts: int = 1,
    window_days: int = 14,
) -> bool:
    """Check if company has been contacted too recently.

    Args:
        company_id: The company to check
        session: Database session
        max_contacts: Maximum distinct people to contact in the window
        window_days: Lookback window in days

    Returns:
        True if within throttle (allowed to send), False if throttled
    """
    cutoff = datetime.now(UTC) - timedelta(days=window_days)

    # Count distinct people at this company who have received emails in the window
    # This requires joining: Send -> Message -> Person -> Company
    stmt = (
        select(func.count(func.distinct(Person.id)))
        .join(Message, Message.id == Send.message_id)
        .join(Person, Person.id == Message.person_id)
        .where(
            Person.company_id == company_id,
            Send.sent_at >= cutoff,
        )
    )

    count = await session.execute(stmt)
    contacted_count = count.scalar() or 0

    return contacted_count < max_contacts


__all__ = ["check_company_throttle"]
