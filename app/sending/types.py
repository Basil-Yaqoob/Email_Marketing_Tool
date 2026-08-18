"""Types for the sending infrastructure (mailboxes, DNS checks, warmup)."""

from __future__ import annotations

import enum
from dataclasses import dataclass


class CheckStatus(enum.StrEnum):
    """Severity of a DNS check result.

    PASS — the check passed, no action needed.
    WARN — the check passed but with caveats (e.g. p=none on DMARC).
    FAIL — the check failed; sending will not work without fixing this.
    """

    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class DNSCheck:
    """Result of a single DNS record check.

    Attributes:
        record: The type of record ("SPF", "DKIM", "DMARC", "PTR", "MX", "alignment")
        status: PASS | WARN | FAIL
        found: The actual value(s) found, if any (None if not found)
        expected: What we expected to find (None if not applicable)
        fix: A copy-pasteable instruction to fix this, if status is FAIL or WARN
    """

    record: str
    status: CheckStatus
    found: str | None
    expected: str | None
    fix: str


__all__ = ["CheckStatus", "DNSCheck"]
