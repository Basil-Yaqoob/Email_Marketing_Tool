"""Match replies back to the sends that caused them.

Strategies in order of reliability:
  1. Exact: In-Reply-To == stored Message-ID
  2. Exact: References entry == stored Message-ID
  3. Exact: DSN original-message-id
  4. Fuzzy: from + subject similarity within window (lower confidence)
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.send import Send
from app.replies.types import MatchConfidence, RawMessage

if TYPE_CHECKING:
    pass


async def match_to_send(
    msg: RawMessage, session: AsyncSession, window_hours: int = 72
) -> tuple[Send | None, MatchConfidence]:
    """Match a message to a send using exact then fuzzy strategies.

    Args:
        msg: The raw IMAP message
        session: Database session
        window_hours: How far back to look for fuzzy matches (default 72 hours)

    Returns:
        (Send if matched, confidence level) or (None, _) if unmatched
    """
    # Strategy 1: In-Reply-To matches exactly
    if msg.in_reply_to:
        send = await _find_send_by_message_id(msg.in_reply_to, session)
        if send:
            return send, MatchConfidence.EXACT

    # Strategy 2: References chain contains one of our Message-IDs
    if msg.references:
        for ref in msg.references:
            send = await _find_send_by_message_id(ref, session)
            if send:
                return send, MatchConfidence.EXACT

    # Strategy 3: DSN original-message-id (would be extracted from raw_body in real impl)
    # This requires email parsing which is deferred to the IMAP client
    # For now, just document that it would go here

    # Strategy 4: Fuzzy match on from + subject + time window (lowest confidence)
    send = await _fuzzy_match(msg, session, window_hours)
    if send:
        return send, MatchConfidence.FUZZY

    # No match found
    return None, MatchConfidence.EXACT  # confidence is meaningless when no match


async def _find_send_by_message_id(message_id: str, session: AsyncSession) -> Send | None:
    """Look up a Send by its RFC Message-ID header."""
    # Normalize: Message-ID can have angle brackets
    normalized_id = message_id.strip("<>")

    row = await session.execute(select(Send).where(Send.message_id_header == normalized_id))
    return row.scalar_one_or_none()


async def _fuzzy_match(msg: RawMessage, session: AsyncSession, window_hours: int) -> Send | None:
    """Fuzzy match on from address, subject similarity, and time window.

    This is a last resort. Requires:
      - Exact from address match
      - Subject similarity (after stripping Re:/Fwd:/etc.)
      - Message received within window_hours of send
    """
    # Extract recipient email from sender of reply (fuzzy match on recipient)
    # Since we don't have the original recipient email in the RawMessage,
    # this would require querying the Send's Message to get the recipient.
    # For now, return None since we need that enrichment.
    # In a real implementation, we'd join to Message and EmailAddress tables.

    return None


def strip_reply_prefix(subject: str) -> str:
    """Remove Re:, Fwd:, AW:, etc. prefixes from subject line.

    Handles: Re:, RE:, Fwd:, FWD:, AW:, and their variants with brackets.
    """
    # Pattern matches various reply prefixes in multiple languages
    cleaned = re.sub(
        r"^(?:\[.*?\]\s*)?(?:Re|RE|Fwd|FWD|AW|Aw)?\s*[:>]*\s*", "", subject, flags=re.IGNORECASE
    )
    return cleaned.strip()


def subject_similarity(subject1: str, subject2: str) -> float:
    """Calculate subject line similarity (0.0 to 1.0).

    After stripping prefixes, compare for equality or containment.
    """
    s1 = strip_reply_prefix(subject1).lower()
    s2 = strip_reply_prefix(subject2).lower()

    if s1 == s2:
        return 1.0

    # One contains the other (e.g. "Follow-up" in "Re: Follow-up")
    if s1 in s2 or s2 in s1:
        return 0.8

    # No meaningful similarity
    return 0.0


__all__ = ["match_to_send", "strip_reply_prefix", "subject_similarity"]
