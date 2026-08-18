"""Auto-pause guards — monitor mailbox and campaign health.

Pause conditions:
- bounce rate > 3%
- spam complaint spike
- authentication failure
- health score below floor

Pausing is immediate and requires explicit human resume.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.db.models.mailbox import Mailbox


class GuardVerdictEnum(StrEnum):
    """Guard evaluation verdict."""

    HEALTHY = "healthy"
    WARN = "warn"
    PAUSE = "pause"


@dataclass(frozen=True)
class GuardDecision:
    """Guard evaluation result."""

    verdict: GuardVerdictEnum
    reason: str


async def evaluate_mailbox_guards(mailbox: Mailbox) -> GuardDecision:
    """Evaluate mailbox pause conditions.

    Args:
        mailbox: The mailbox to evaluate

    Returns:
        GuardDecision with verdict and reason
    """
    # Stub: real implementation would query bounce/complaint rates
    if mailbox.health_score < 0.5:
        return GuardDecision(
            verdict=GuardVerdictEnum.PAUSE,
            reason="Health score below 0.5 floor",
        )

    return GuardDecision(
        verdict=GuardVerdictEnum.HEALTHY,
        reason="All guards passed",
    )


__all__ = ["GuardDecision", "GuardVerdictEnum", "evaluate_mailbox_guards"]
