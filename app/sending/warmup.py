"""Warmup ramp schedule for new mailboxes.

Gradual volume increase helps establish sending reputation. Starts low (~5/day),
increases over time, but respects provider caps. Self-warmup means the user's
own mailboxes email each other.

Warmup is a baseline, not a guarantee — modern providers detect synthetic
engagement patterns. The real drivers are domain age, slow ramps, genuine
replies, and clean initial sends (no links).
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.db.models.enums import MailboxProvider
from app.db.models.mailbox import Mailbox

WARMUP_STAGES = {
    # (stage, min_days, daily_cap): stage 0-6 over 42 days
    0: (1, 5),  # Days 0-5: 5/day
    1: (6, 10),  # Days 6-11: 10/day
    2: (12, 20),  # Days 12-17: 20/day
    3: (18, 40),  # Days 18-23: 40/day
    4: (24, 75),  # Days 24-29: 75/day
    5: (30, 150),  # Days 30-35: 150/day
    6: (36, 300),  # Days 36+: 300/day (stable)
}

PROVIDER_CAPS = {
    # Maximum daily sends per provider at full ramp
    MailboxProvider.GOOGLE_WORKSPACE: 2000,  # Google's soft limit
    MailboxProvider.MICROSOFT_365: 1500,  # Microsoft's soft limit
    MailboxProvider.GENERIC_SMTP: 1000,  # Cautious default
}


def next_warmup_stage(mailbox: Mailbox) -> int:
    """Determine which warmup stage this mailbox is in, 0-6.

    Based on age. Later stages cap out at 300/day.
    """
    age = datetime.now(UTC) - mailbox.created_at
    stage = 0
    for s, (min_days, _) in sorted(WARMUP_STAGES.items()):
        if age.days >= min_days:
            stage = s
    return stage


def daily_cap_for_stage(mailbox: Mailbox) -> int:
    """Daily send cap for this mailbox's current warmup stage and provider.

    Respects both the warmup ramp and the provider's hard limit.
    """
    current_stage = next_warmup_stage(mailbox)
    _, warmup_cap = WARMUP_STAGES[current_stage]
    provider_cap = PROVIDER_CAPS.get(mailbox.provider, 1000)
    return min(warmup_cap, provider_cap)


def warmup_advice() -> str:
    """Text for the UI explaining what warmup does and doesn't guarantee."""
    return (
        "Warmup builds a baseline, it does not guarantee inbox placement. "
        "Providers increasingly detect synthetic engagement patterns. "
        "What actually moves deliverability: domain age (buy 3-4 weeks before use), "
        "a slow volume ramp, genuine reply rates, and no links in the first email."
    )


__all__ = [
    "PROVIDER_CAPS",
    "WARMUP_STAGES",
    "daily_cap_for_stage",
    "next_warmup_stage",
    "warmup_advice",
]
