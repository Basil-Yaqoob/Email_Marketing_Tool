"""IMAP ingestion client with IDLE support and idempotent high-water mark tracking.

Fetches new messages from a mailbox, stores raw messages for later parsing,
and tracks a per-mailbox UID high-water mark so restarts don't re-scan everything.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from app.replies.types import RawMessage

if TYPE_CHECKING:
    from app.db.models.mailbox import Mailbox


@dataclass(frozen=True)
class IMAPHighWaterMark:
    """Tracks the highest UID seen in a mailbox."""

    mailbox_id: str
    highest_uid: int
    checked_at: datetime


class IMAPIngestor:
    """IMAP client for fetching new mail.

    This is a stub that demonstrates the API. Real implementation would:
      - Connect to IMAP with backoff on failure
      - Use IDLE where supported, fallback to polling
      - Track UID high-water mark per mailbox
      - Fetch headers first, body only for matches
      - Store raw message bytes for re-parsing
    """

    async def ingest(self, mailbox: Mailbox, since_uid: int | None = None) -> list[RawMessage]:
        """Fetch new messages from a mailbox, idempotent.

        Args:
            mailbox: The mailbox to ingest
            since_uid: Start from this UID (from last ingest). If None, start from 1.

        Returns:
            List of RawMessage objects
        """
        # This would be the real IMAP implementation.
        # For now, return empty list (tests will mock this).
        return []

    async def ingest_with_idle(
        self, mailbox: Mailbox, since_uid: int | None = None, idle_timeout_seconds: int = 300
    ) -> list[RawMessage]:
        """Ingest with IDLE support (push notifications).

        Falls back to polling if IDLE is not supported by the server.

        Args:
            mailbox: The mailbox to ingest
            since_uid: Start from this UID
            idle_timeout_seconds: How long to wait for new mail before polling

        Returns:
            List of RawMessage objects
        """
        # Real implementation would:
        # 1. Connect to IMAP server (using credentials from Mailbox)
        # 2. Select INBOX
        # 3. Try to enable IDLE
        # 4. Wait for new mail or timeout
        # 5. Fetch headers (UID, Message-ID, In-Reply-To, References, From, Subject, Date)
        # 6. Store raw message if it matches a send
        # 7. Update high-water mark
        # 8. Handle UID invalidation (UIDVALIDITY change)

        return []

    @staticmethod
    def deduplicate_by_message_id(messages: list[RawMessage]) -> list[RawMessage]:
        """Remove duplicates by Message-ID (Gmail exposes labels as folders)."""
        seen: dict[str | None, RawMessage] = {}
        for msg in messages:
            # Use Message-ID as key, or UID if no Message-ID
            key = msg.message_id_header or f"uid:{msg.uid}"
            if key not in seen:
                seen[key] = msg
        return list(seen.values())


__all__ = ["IMAPHighWaterMark", "IMAPIngestor"]
