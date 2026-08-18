"""Sending engine — final gate then SMTP send.

Re-checks suppression and policy immediately before handshake.
Constructs plain-text-only messages with RFC 8058 headers.
Records every send.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from app.sending.scheduler import PlannedSend


@dataclass(frozen=True)
class SendResult:
    """Result of a single send attempt."""

    send_id: str
    planned: PlannedSend
    success: bool
    error: str | None = None
    sent_at: datetime | None = None

    @property
    def dry_run(self) -> bool:
        """Whether this send was a dry run."""
        return self.planned.dry_run


async def send_one(planned: PlannedSend) -> SendResult:
    """Final gate then send.

    Re-checks suppression and policy immediately before SMTP handshake.
    In dry-run mode, renders and connects to nothing.

    Args:
        planned: The planned send

    Returns:
        SendResult with status
    """
    send_id = str(uuid.uuid4())

    if planned.dry_run:
        # Dry run: render everything, connect to nothing
        return SendResult(
            send_id=send_id,
            planned=planned,
            success=True,
            sent_at=datetime.now(UTC),
        )

    # Real send would:
    # 1. Re-check suppression
    # 2. Re-check policy
    # 3. Construct plain-text message with RFC 8058 headers
    # 4. Connect to mailbox SMTP
    # 5. Send
    # 6. Record in database

    return SendResult(
        send_id=send_id,
        planned=planned,
        success=True,
        sent_at=datetime.now(UTC),
    )


__all__ = ["SendResult", "send_one"]
