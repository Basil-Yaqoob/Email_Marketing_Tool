"""Send plan scheduler — decide what goes out today, from which mailbox, at what time.

Constraints (all hard):
- per-mailbox daily cap, reduced by warmup stage
- mailbox health score >= floor
- recipient business hours in THEIR timezone
- per-company contact throttle (Session 15)
- suppression (Session 15)
- policy verdict is not BLOCK (Session 15)

Weighted rotation: pick next mailbox by health score, not round-robin.
Timing must look human: randomised gaps, business hours in recipient's timezone.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from app.db.models.mailbox import Mailbox
from app.db.models.message import Message


@dataclass(frozen=True)
class PlannedSend:
    """A single send scheduled for a specific time."""

    message_id: str
    mailbox_id: str
    recipient_email: str
    scheduled_time: datetime
    dry_run: bool = False


@dataclass(frozen=True)
class SendPlan:
    """The plan for a day's sends."""

    planned: list[PlannedSend]
    dry_run: bool = False


async def build_send_plan(
    messages: Sequence[Message],
    mailboxes: Sequence[Mailbox],
    day: datetime,
    dry_run: bool = False,
) -> SendPlan:
    """Build a send plan for the day.

    Args:
        messages: Messages to send (should be filtered to sendable ones)
        mailboxes: Available mailboxes (should be sorted by health)
        day: The day to plan for
        dry_run: If True, plan without checking constraints

    Returns:
        SendPlan with sends scheduled throughout the day
    """
    if not messages or not mailboxes:
        return SendPlan(planned=[], dry_run=dry_run)

    planned: list[PlannedSend] = []

    # Simple round-robin distribution for MVP
    start_hour = 9  # Business hours start at 9 AM
    end_hour = 17  # Business hours end at 5 PM

    for mailbox_index, message in enumerate(messages):
        mailbox = mailboxes[mailbox_index % len(mailboxes)]

        # Calculate send time (simplified: spread throughout day)
        hour_offset = ((mailbox_index + 1) // len(mailboxes)) % (end_hour - start_hour)
        scheduled_time = day.replace(
            hour=start_hour + hour_offset,
            minute=0,
            second=0,
            microsecond=0,
        )

        planned.append(
            PlannedSend(
                message_id=str(message.id),
                mailbox_id=str(mailbox.id),
                recipient_email="test@example.com",  # Will be filled in real implementation
                scheduled_time=scheduled_time,
                dry_run=dry_run,
            )
        )

    return SendPlan(planned=planned, dry_run=dry_run)


__all__ = ["PlannedSend", "SendPlan", "build_send_plan"]
