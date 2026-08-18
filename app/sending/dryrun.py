"""Dry run mode — render exactly what would be sent, connect to nothing.

This is what makes the first real campaign safe.
"""

from __future__ import annotations

from app.sending.scheduler import PlannedSend


def render_dry_run(planned: PlannedSend) -> str:
    """Render a planned send for dry run inspection.

    Args:
        planned: The planned send

    Returns:
        String representation of what would be sent
    """
    return (
        f"Dry Run: {planned.message_id}\n"
        f"Mailbox: {planned.mailbox_id}\n"
        f"Recipient: {planned.recipient_email}\n"
        f"Scheduled: {planned.scheduled_time}\n"
    )


__all__ = ["render_dry_run"]
