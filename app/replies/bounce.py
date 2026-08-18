"""Bounce parsing — DSN, diagnostic codes, and provider-specific patterns.

The standard bounce format is RFC 3464 (Delivery Status Notification).
Not all providers send standard DSN; fall back to pattern matching.
"""

from __future__ import annotations

import re

from app.replies.types import BounceInfo, BounceKind


def parse_bounce(raw_body: str, from_address: str) -> BounceInfo | None:
    """Parse a bounce message, returning BounceInfo or None if not a bounce.

    Tries in order:
      1. DSN part with diagnostic code
      2. Google/Microsoft/Proofpoint text patterns
      3. Common generic bounce phrases

    Falls back to None if unrecognizable rather than guessing wrong.
    """
    # Try DSN first (most reliable)
    dsn_bounce = _parse_dsn(raw_body, from_address)
    if dsn_bounce:
        return dsn_bounce

    # Try provider-specific patterns
    google_bounce = _parse_google_bounce(raw_body, from_address)
    if google_bounce:
        return google_bounce

    microsoft_bounce = _parse_microsoft_bounce(raw_body, from_address)
    if microsoft_bounce:
        return microsoft_bounce

    proofpoint_bounce = _parse_proofpoint_bounce(raw_body, from_address)
    if proofpoint_bounce:
        return proofpoint_bounce

    # Generic patterns (low confidence)
    generic_bounce = _parse_generic_bounce(raw_body, from_address)
    if generic_bounce:
        return generic_bounce

    # Not a recognizable bounce
    return None


def _parse_dsn(body: str, address: str) -> BounceInfo | None:
    """Parse RFC 3464 Delivery Status Notification.

    DSN has a message/delivery-status part with a Status header.
    """
    # Look for Status: X.Y.Z pattern
    status_match = re.search(r"Status:\s*(\d+\.\d+\.\d+)", body, re.IGNORECASE)
    if not status_match:
        return None

    status_code = status_match.group(1)
    kind = BounceKind.HARD if status_code.startswith("5") else BounceKind.SOFT

    # Look for Diagnostic-Code header
    diagnostic_match = re.search(
        r"Diagnostic-Code:\s*(?:smtp;\s*)?(.+?)(?:\r?\n|$)", body, re.IGNORECASE
    )
    reason = diagnostic_match.group(1).strip() if diagnostic_match else f"Status {status_code}"

    return BounceInfo(kind=kind, status_code=status_code, reason=reason, address=address)


def _parse_google_bounce(body: str, address: str) -> BounceInfo | None:
    """Parse Google Mail bounce patterns.

    Patterns like:
      "The email account that you tried to reach does not exist"
      "The user you are trying to contact is receiving mail too quickly"
    """
    hard_patterns = [
        r"(?:The email account that you tried to reach does not exist|User unknown)",
        r"(?:invalid address|Address rejected)",
        r"(?:The user account is disabled|Account has been locked)",
    ]

    soft_patterns = [
        r"(?:is not accepting email|receiving mail too quickly)",
        r"(?:try again later|temporarily unavailable)",
        r"(?:quota exceeded|mailbox full)",
    ]

    for pattern in hard_patterns:
        if re.search(pattern, body, re.IGNORECASE):
            return BounceInfo(
                kind=BounceKind.HARD,
                status_code="5.1.1",
                reason=f"Google: {pattern}",
                address=address,
            )

    for pattern in soft_patterns:
        if re.search(pattern, body, re.IGNORECASE):
            return BounceInfo(
                kind=BounceKind.SOFT,
                status_code="4.4.2",
                reason=f"Google: {pattern}",
                address=address,
            )

    return None


def _parse_microsoft_bounce(body: str, address: str) -> BounceInfo | None:
    """Parse Microsoft Exchange bounce patterns.

    Patterns like:
      "550 5.1.1 The email account does not exist"
      "452 4.2.2 The mailbox is full"
    """
    hard_patterns = [
        r"(?:550|5\.1\.1).*?(?:does not exist|undeliverable|not a known|rejected)",
        r"(?:user not found|unknown recipient)",
        r"(?:invalid recipient|Address rejected)",
    ]

    soft_patterns = [
        r"(?:452|4\.2\.2).*?(?:mailbox full|quota|space)",
        r"(?:421|4\.\d\.\d).*?(?:try again later|temporarily)",
    ]

    for pattern in hard_patterns:
        if re.search(pattern, body, re.IGNORECASE):
            return BounceInfo(
                kind=BounceKind.HARD,
                status_code="5.1.1",
                reason=f"Microsoft: {pattern}",
                address=address,
            )

    for pattern in soft_patterns:
        if re.search(pattern, body, re.IGNORECASE):
            return BounceInfo(
                kind=BounceKind.SOFT,
                status_code="4.2.2",
                reason=f"Microsoft: {pattern}",
                address=address,
            )

    return None


def _parse_proofpoint_bounce(body: str, address: str) -> BounceInfo | None:
    """Parse Proofpoint/Barracuda bounce patterns.

    Patterns like:
      "550 5.1.1 The user does not exist"
    """
    hard_patterns = [
        r"(?:550|5\.1\.1).*?(?:does not exist|unknown|rejected|invalid)",
    ]

    soft_patterns = [
        r"(?:451|452|4\.\d\.\d).*?(?:try again|unavailable|temporarily)",
    ]

    for pattern in hard_patterns:
        if re.search(pattern, body, re.IGNORECASE):
            return BounceInfo(
                kind=BounceKind.HARD,
                status_code="5.1.1",
                reason=f"Proofpoint: {pattern}",
                address=address,
            )

    for pattern in soft_patterns:
        if re.search(pattern, body, re.IGNORECASE):
            return BounceInfo(
                kind=BounceKind.SOFT,
                status_code="4.4.2",
                reason=f"Proofpoint: {pattern}",
                address=address,
            )

    return None


def _parse_generic_bounce(body: str, address: str) -> BounceInfo | None:
    """Parse very generic bounce phrases as a last resort.

    This is low-confidence — prefer the patterns above.
    """
    # Hard bounce indicators
    if re.search(r"(?:permanent\s+failure|no such|does not exist)", body, re.IGNORECASE):
        return BounceInfo(
            kind=BounceKind.HARD,
            status_code=None,
            reason="Permanent delivery failure",
            address=address,
        )

    # Soft bounce indicators
    if re.search(r"(?:temporarily unavailable|try again|deferred)", body, re.IGNORECASE):
        return BounceInfo(
            kind=BounceKind.SOFT,
            status_code=None,
            reason="Temporary delivery failure",
            address=address,
        )

    return None


__all__ = ["BounceInfo", "BounceKind", "parse_bounce"]
