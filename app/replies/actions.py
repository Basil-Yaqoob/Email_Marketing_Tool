"""Execute actions based on reply classification.

Each reply class has specific actions:
- INTERESTED: Halt, flag for human
- NOT_INTERESTED: Halt, suppress
- UNSUBSCRIBE: Halt immediately, suppress (address and possibly domain)
- OUT_OF_OFFICE: Don't halt, reschedule after return date
- WRONG_PERSON: Halt for this person, extract referral, re-enter pipeline
- AUTO_REPLY: No action
- BOUNCE: Handled in Session 17
- UNCLEAR: Halt (safe default), human queue
"""

# ruff: noqa: SIM110

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.replies.classification import ReplyClass


@dataclass(frozen=True)
class ActionResult:
    """Result of processing a reply."""

    class_action: str  # What was done (e.g., "halted", "suppressed", "rescheduled")
    halt_sequence: bool  # Whether to stop sending follow-ups
    suppress_address: bool  # Whether to add to suppression list
    suppress_domain: bool  # Whether to suppress entire domain
    reschedule_date: str | None  # ISO date if rescheduling (OOO)
    human_review: bool  # Whether to flag for human
    notifications: list[str] = field(default_factory=list)  # Messages to send


def get_actions(reply_class: ReplyClass) -> ActionResult:
    """Determine actions for a reply class.

    Args:
        reply_class: The classified reply type

    Returns:
        ActionResult describing what to do
    """
    if reply_class == ReplyClass.INTERESTED:
        return ActionResult(
            class_action="interested_halt_flag",
            halt_sequence=True,
            suppress_address=False,
            suppress_domain=False,
            reschedule_date=None,
            human_review=True,
            notifications=["Lead expressed interest"],
        )

    elif reply_class == ReplyClass.NOT_INTERESTED:
        return ActionResult(
            class_action="not_interested_suppress",
            halt_sequence=True,
            suppress_address=True,
            suppress_domain=False,
            reschedule_date=None,
            human_review=False,
            notifications=[],
        )

    elif reply_class == ReplyClass.UNSUBSCRIBE:
        return ActionResult(
            class_action="unsubscribe_suppress",
            halt_sequence=True,
            suppress_address=True,
            suppress_domain=False,  # Set to True if wording implies whole company
            reschedule_date=None,
            human_review=False,  # Compliance action, no review needed
            notifications=["Unsubscribe processed"],
        )

    elif reply_class == ReplyClass.OUT_OF_OFFICE:
        # OOO does NOT halt; sequence resumes after return date
        return ActionResult(
            class_action="ooo_reschedule",
            halt_sequence=False,
            suppress_address=False,
            suppress_domain=False,
            reschedule_date=None,  # Caller sets this after parsing return date
            human_review=False,
            notifications=[],
        )

    elif reply_class == ReplyClass.WRONG_PERSON:
        # Halt for this person, but referral will be queued as new lead
        return ActionResult(
            class_action="wrong_person_extract_referral",
            halt_sequence=True,
            suppress_address=False,
            suppress_domain=False,
            reschedule_date=None,
            human_review=False,  # Referral re-enters pipeline automatically
            notifications=[],
        )

    elif reply_class == ReplyClass.AUTO_REPLY:
        # No action — don't count as engagement, don't halt
        return ActionResult(
            class_action="auto_reply_ignore",
            halt_sequence=False,
            suppress_address=False,
            suppress_domain=False,
            reschedule_date=None,
            human_review=False,
            notifications=[],
        )

    elif reply_class == ReplyClass.BOUNCE:
        # Handled in Session 17
        return ActionResult(
            class_action="bounce_handled",
            halt_sequence=False,  # Session 17 already marked invalid
            suppress_address=False,
            suppress_domain=False,
            reschedule_date=None,
            human_review=False,
            notifications=[],
        )

    else:  # UNCLEAR or other
        # Safe default: halt sequence and send to human
        return ActionResult(
            class_action="unclear_halt_review",
            halt_sequence=True,
            suppress_address=False,
            suppress_domain=False,
            reschedule_date=None,
            human_review=True,
            notifications=["Unclear reply — manual review required"],
        )


def should_suppress_domain(body: str, address: str) -> bool:
    """Determine if unsubscribe wording implies suppressing the whole domain.

    Phrases like "remove us from your list" suggest company-wide suppression,
    while "remove me" suggests just the individual.
    """
    company_wide_patterns = [
        r"(?:remove\s+)?(?:us|our\s+company|our\s+organization)",
        r"take\s+(?:us|our\s+company)",
        r"stop\s+(?:emailing|contacting)\s+(?:us|our\s+company)",
    ]

    for pattern in company_wide_patterns:
        if re.search(pattern, body, re.IGNORECASE):
            return True

    return False


__all__ = ["ActionResult", "get_actions", "should_suppress_domain"]
