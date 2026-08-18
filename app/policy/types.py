"""Types for the policy engine."""

from __future__ import annotations

import enum
from dataclasses import dataclass


class Verdict(enum.StrEnum):
    """Jurisdiction policy verdict."""

    ALLOW = "allow"
    ALLOW_WITH_REQ = "allow_with_requirements"
    WARN = "warn"
    BLOCK = "block"


class SuppressionScope(enum.StrEnum):
    """Scope of a suppression entry."""

    ADDRESS = "address"  # this person
    DOMAIN = "domain"  # everyone at this company
    GLOBAL = "global"  # across all campaigns


@dataclass(frozen=True)
class SuppressionHit:
    """A suppression was found."""

    value: str  # the suppressed address or domain
    scope: SuppressionScope
    reason: str
    added_at: str  # ISO format


@dataclass(frozen=True)
class PolicyDecision:
    """The result of evaluating a lead against jurisdiction rules."""

    verdict: Verdict
    requirements: list[str]  # what this jurisdiction requires
    reason: str  # plain English explanation for the user


@dataclass(frozen=True)
class LintFinding:
    """A single lint violation."""

    level: str  # "block" | "warn"
    rule: str  # e.g. "missing_address"
    message: str  # human-readable explanation


@dataclass(frozen=True)
class LintReport:
    """Complete lint results for a message."""

    findings: list[LintFinding]

    @property
    def has_blocks(self) -> bool:
        """True if any findings are BLOCK level."""
        return any(f.level == "block" for f in self.findings)

    @property
    def has_warnings(self) -> bool:
        """True if any findings are WARN level."""
        return any(f.level == "warn" for f in self.findings)


__all__ = [
    "LintFinding",
    "LintReport",
    "PolicyDecision",
    "SuppressionHit",
    "SuppressionScope",
    "Verdict",
]
