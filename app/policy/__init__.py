"""Policy engine: jurisdiction rules, suppression, linting, and compliance."""

from app.policy.jurisdiction import PolicyDecision, Verdict, evaluate
from app.policy.linter import LintFinding, LintReport, lint
from app.policy.suppression import SuppressionHit, SuppressionScope, add_suppression, is_suppressed
from app.policy.throttle import check_company_throttle
from app.policy.unsubscribe import UnsubscribeToken, generate_unsubscribe_headers

__all__ = [
    "LintFinding",
    "LintReport",
    "PolicyDecision",
    "SuppressionHit",
    "SuppressionScope",
    "UnsubscribeToken",
    "Verdict",
    "add_suppression",
    "check_company_throttle",
    "evaluate",
    "generate_unsubscribe_headers",
    "is_suppressed",
    "lint",
]
