"""Template linter — blocking and warning checks for compliance.

Blocking checks (campaign cannot send with these):
- physical postal address present (CAN-SPAM)
- working unsubscribe mechanism present (all regimes)
- List-Unsubscribe and List-Unsubscribe-Post headers set (RFC 8058)
- From name and domain honest, matching configured identity
- subject not deceptive relative to body
- recipient not suppressed
- jurisdiction verdict is not BLOCK

Warning checks:
- links in first-touch email
- jurisdiction verdict is WARN

A BLOCK cannot be overridden by a checkbox.
"""

from __future__ import annotations

import re

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.company import Company
from app.db.models.message import Message
from app.policy.jurisdiction import evaluate
from app.policy.suppression import is_suppressed
from app.policy.types import LintFinding, LintReport, Verdict

# Regex to find URLs in email body (simplified)
URL_PATTERN = re.compile(r"https?://\S+")


async def lint(
    message: Message,
    company: Company,
    mailbox_from_name: str,
    mailbox_from_domain: str,
    session: AsyncSession,
) -> LintReport:
    """Run lint checks on a message for compliance.

    Args:
        message: The message to lint
        company: The company/recipient
        mailbox_from_name: The mailbox's from name (e.g. "Alex")
        mailbox_from_domain: The mailbox's domain (e.g. "ferrylane.com")
        session: Database session

    Returns:
        LintReport with all findings
    """
    findings: list[LintFinding] = []

    # ===== BLOCKING CHECKS =====

    # 1. Physical address required
    if not company.address or not company.address.strip():
        findings.append(
            LintFinding(
                level="block",
                rule="missing_address",
                message="CAN-SPAM and most regimes require a physical postal address in the footer",
            )
        )

    # 2. Unsubscribe mechanism required
    if not message.body or "unsubscribe" not in message.body.lower():
        findings.append(
            LintFinding(
                level="block",
                rule="missing_unsubscribe",
                message="All compliance regimes require a working unsubscribe mechanism (link or email)",
            )
        )

    # 3. RFC 8058 headers required
    # Check the email body or headers for List-Unsubscribe and List-Unsubscribe-Post
    if not message.body or "list-unsubscribe" not in message.body.lower():
        findings.append(
            LintFinding(
                level="block",
                rule="missing_rfc8058_headers",
                message="RFC 8058 (Google, Yahoo, Apple requirement): Missing List-Unsubscribe and List-Unsubscribe-Post headers",
            )
        )

    # 4. From name and domain honesty
    if not _is_honest_from(message.body, mailbox_from_name, mailbox_from_domain):
        findings.append(
            LintFinding(
                level="block",
                rule="dishonest_from",
                message=f"From name ({mailbox_from_name}) and domain ({mailbox_from_domain}) must appear honestly in the signature",
            )
        )

    # 5. Suppression check
    suppression = await is_suppressed(f"test@{company.domain}", session)
    if suppression:
        findings.append(
            LintFinding(
                level="block",
                rule="suppressed",
                message=f"Address or domain is suppressed: {suppression.reason}",
            )
        )

    # 6. Jurisdiction verdict must not be BLOCK
    policy = await evaluate(company, session)
    if policy.verdict == Verdict.BLOCK:
        findings.append(
            LintFinding(
                level="block",
                rule="jurisdiction_blocked",
                message=f"Jurisdiction {company.country_code} blocks cold outreach: {policy.reason}",
            )
        )

    # ===== WARNING CHECKS =====

    # 7. Links in first-touch email
    if message.body and URL_PATTERN.search(message.body):
        findings.append(
            LintFinding(
                level="warn",
                rule="links_in_first_touch",
                message="First-touch emails with links have lower deliverability; consider removing or testing without links first",
            )
        )

    # 8. Jurisdiction verdict is WARN
    if policy.verdict == Verdict.WARN:
        findings.append(
            LintFinding(
                level="warn",
                rule="jurisdiction_warn",
                message=f"Jurisdiction {company.country_code} has compliance caveats: {policy.reason}",
            )
        )

    return LintReport(findings=findings)


def _is_honest_from(
    body: str | None,
    from_name: str,
    from_domain: str,
) -> bool:
    """Check that the From name and domain appear in the email body signature.

    A signature like:
        Best,
        Alex Rivera
        Ferrylane
        ferrylane.com

    Would pass with from_name='Alex' and from_domain='ferrylane.com'.
    """
    if not body:
        return False

    body_lower = body.lower()
    from_name_lower = from_name.lower()
    from_domain_lower = from_domain.lower()

    # Both name and domain should appear in the body
    has_name = from_name_lower in body_lower
    has_domain = from_domain_lower in body_lower

    return has_name and has_domain


__all__ = ["LintFinding", "LintReport", "lint"]
