"""Referral extraction from wrong-person replies.

Extracts name, email, title, and confidence from phrasings like:
- "You want Sarah Chen, she's our practice manager — sarah@…"
- "Please contact our office manager instead" (no name)
- "I've forwarded this to Tom" (name only)

Never invents an email. If none given, leaves it null for waterfall to find.
"""

# ruff: noqa: RUF001, SIM102

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Referral:
    """Extracted referral from a wrong-person reply."""

    name: str | None  # Person's name
    email: str | None  # Person's email (if given)
    title: str | None  # Job title (if mentioned)
    confidence: float  # 0.0 to 1.0
    raw_snippet: str  # Original text snippet


def extract_referral(body: str) -> Referral | None:
    """Extract a referral from wrong-person reply.

    Returns Referral if one is found, None otherwise.
    """
    # Pattern 1: "contact X at email" or "talk to X (email)"
    # Ensure we capture email without trailing punctuation
    contact_pattern = r"(?:contact|talk\s+to|reach|email|ask\s+for)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+(?:at|@)\s*([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+)(?:[^\w]|$)"
    match = re.search(contact_pattern, body)
    if match:
        name = match.group(1).strip()
        email = match.group(2).strip().rstrip(".")  # Remove trailing period if present
        return Referral(
            name=name,
            email=email,
            title=None,
            confidence=0.95,
            raw_snippet=match.group(0),
        )

    # Pattern 2: "X is our [title]" or "X, [title]"
    title_pattern = r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+(?:is\s+)?(?:our\s+)?([a-z\s]+?)(?:\s+(?:—|–|-|:)\s*([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+))?"
    match = re.search(title_pattern, body)
    if match:
        name = match.group(1).strip()
        title = match.group(2).strip() if match.group(2) else None
        email = match.group(3).strip() if match.group(3) else None
        if name and name not in ("I", "You", "We"):  # Skip pronouns
            return Referral(
                name=name,
                email=email,
                title=title,
                confidence=0.90,
                raw_snippet=match.group(0),
            )

    # Pattern 3: "forwarded to X" or "passed to X" or "talk to X"
    forward_pattern = r"(?:(?:forwarded|passed|sent|cc?ed)\s+(?:to|with)|(?:talk\s+to|want\s+to\s+talk\s+to|reach|contact))\s+([A-Z][a-z]+)(?:\s+[A-Z][a-z]+)?(?:\s+(?:about|regarding|to|at|for)|$|\s)"
    match = re.search(forward_pattern, body, re.IGNORECASE)
    if match:
        name = match.group(1).strip()
        if name not in ("I", "You", "We"):
            return Referral(
                name=name,
                email=None,
                title=None,
                confidence=0.75 if "forwarded" in match.group(0).lower() else 0.70,
                raw_snippet=match.group(0),
            )

    # Pattern 4: Role-only: "contact our sales team" / "reach out to the manager"
    role_pattern = r"(?:contact|reach|talk\s+to|ask\s+for)\s+(?:our\s+)?(?:the\s+)?([a-z\s]+?)(?:\s+(?:at|here|below|above)|for|$)"
    match = re.search(role_pattern, body, re.IGNORECASE)
    if match:
        role = match.group(1).strip()
        # Only return if it's a title-like word (manager, director, etc.)
        title_keywords = ["manager", "director", "head", "lead", "team", "department", "office"]
        if any(keyword in role.lower() for keyword in title_keywords):
            return Referral(
                name=None,
                email=None,
                title=role,
                confidence=0.65,
                raw_snippet=match.group(0),
            )

    return None


def validate_referral(ref: Referral) -> bool:
    """Check if referral is valid (has at least name or email, never invented).

    Never invent an email. If only a name, that's valid (waterfall finds email).
    """
    # At least one field must be populated
    if not ref.name and not ref.email and not ref.title:
        return False

    # Email must be syntactically valid (basic check)
    if ref.email:
        if not re.match(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$", ref.email):
            return False

    return True


__all__ = ["Referral", "extract_referral", "validate_referral"]
