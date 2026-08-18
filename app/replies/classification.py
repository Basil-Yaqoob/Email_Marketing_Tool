"""Reply classification types and deterministic rules.

Deterministic classification runs first (headers, unambiguous phrases).
Only genuinely ambiguous replies reach the LLM.
Low confidence always routes to human queue, never guessed.
"""

# ruff: noqa: SIM110

from __future__ import annotations

import enum
import re
from dataclasses import dataclass


class ReplyClass(enum.StrEnum):
    """Classification buckets for incoming replies."""

    INTERESTED = "interested"
    NOT_INTERESTED = "not_interested"
    OUT_OF_OFFICE = "out_of_office"
    WRONG_PERSON = "wrong_person"  # Includes referrals
    UNSUBSCRIBE = "unsubscribe"
    AUTO_REPLY = "auto_reply"  # Non-OOO automation
    BOUNCE = "bounce"  # From Session 17
    UNCLEAR = "unclear"  # Low confidence → human queue


@dataclass(frozen=True)
class Classification:
    """Classified reply with confidence and extracted fields."""

    reply_class: ReplyClass
    confidence: float  # 0.0 to 1.0; <0.7 → UNCLEAR
    extracted_fields: dict[str, str | None] | None = None  # e.g., return_date for OOO
    reason: str = ""  # Why this classification


def classify_deterministic(
    from_address: str, subject: str, body: str, headers: dict[str, str]
) -> Classification | None:
    """Apply deterministic rules. Returns Classification or None if ambiguous.

    Rules (in order):
      1. Auto-Submitted or Precedence: bulk → AUTO_REPLY or OUT_OF_OFFICE
      2. Explicit unsubscribe phrases → UNSUBSCRIBE
      3. Bounce markers (handled in Session 17) → BOUNCE
      4. None → return None for LLM classification
    """
    # Rule 1: Automation headers
    auto_submitted = headers.get("auto-submitted", "").lower()
    if auto_submitted and auto_submitted != "no":
        # Could be OOO or generic auto-reply
        if _is_ooo_marker(body):
            return Classification(
                reply_class=ReplyClass.OUT_OF_OFFICE,
                confidence=0.95,
                reason="Auto-Submitted header with OOO markers",
            )
        return Classification(
            reply_class=ReplyClass.AUTO_REPLY,
            confidence=0.95,
            reason="Auto-Submitted header",
        )

    x_autoreply = headers.get("x-autoreply", "").lower()
    if x_autoreply == "yes":
        if _is_ooo_marker(body):
            return Classification(
                reply_class=ReplyClass.OUT_OF_OFFICE,
                confidence=0.95,
                reason="X-Autoreply header with OOO markers",
            )
        # Only classify as AUTO_REPLY if it doesn't have real OOO content
        return Classification(
            reply_class=ReplyClass.AUTO_REPLY,
            confidence=0.90,
            reason="X-Autoreply header",
        )

    precedence = headers.get("precedence", "").lower()
    if precedence in ("bulk", "auto"):
        return Classification(
            reply_class=ReplyClass.AUTO_REPLY,
            confidence=0.90,
            reason="Precedence: bulk/auto header",
        )

    # Rule 2: Explicit unsubscribe phrases
    unsub_match = _match_unsubscribe_phrase(body)
    if unsub_match:
        return Classification(
            reply_class=ReplyClass.UNSUBSCRIBE,
            confidence=0.98,
            reason=unsub_match,
        )

    # Rule 3: Bounce (handled in Session 17, but documented here)
    # A bounce would have bounce_info set by Session 17's ingestion

    # No deterministic match
    return None


def _is_ooo_marker(body: str) -> bool:
    """Check if body contains genuine out-of-office markers.

    Looks for actual OOO content, not just generic automation markers.
    """
    ooo_patterns = [
        r"(?:out\s+of\s+(?:the\s+)?office)",
        r"(?:out of office)",
        r"(?:away|on leave|on vacation)",
        r"(?:back on|returning|i will return|i'm back)",
        r"(?:back\s+to\s+(?:the\s+)?office)",
    ]
    for pattern in ooo_patterns:
        if re.search(pattern, body, re.IGNORECASE):
            return True
    return False


def _match_unsubscribe_phrase(body: str) -> str | None:
    """Match explicit unsubscribe phrases. Returns the phrase or None."""
    unsubscribe_patterns = [
        (r"remove\s+(?:me|us)\s+from\s+(?:your\s+)?(?:mailing\s+)?list", "Remove from list"),
        (r"unsubscribe", "Unsubscribe request"),
        (r"take\s+(?:me|us)\s+off\s+(?:your\s+)?(?:mailing\s+)?list", "Take off list"),
        (r"stop\s+(?:sending|emailing)\s+(?:me|us)", "Stop sending"),
        (r"do\s+not\s+(?:email|contact|send)", "Do not contact"),
    ]
    for pattern, phrase in unsubscribe_patterns:
        if re.search(pattern, body, re.IGNORECASE):
            return phrase
    return None


__all__ = ["Classification", "ReplyClass", "classify_deterministic"]
