"""DNS preflight checks for mailbox readiness.

Checks SPF, DKIM, DMARC, PTR, MX, and From-domain alignment. Every failing
check includes copy-pasteable fix text.
"""

from __future__ import annotations

from dataclasses import dataclass

import dns.exception
import dns.rdatatype
import dns.resolver
from dns.rdataclass import IN

from app.db.models.enums import MailboxProvider
from app.sending.types import CheckStatus, DNSCheck

# DKIM selectors per provider
DKIM_SELECTORS = {
    MailboxProvider.GOOGLE_WORKSPACE: ["google"],
    MailboxProvider.MICROSOFT_365: ["selector1", "selector2"],
    MailboxProvider.GENERIC_SMTP: ["default"],  # No standard; user must specify
}


@dataclass(frozen=True)
class DNSPreflight:
    """Results of all DNS checks for a mailbox."""

    checks: list[DNSCheck]

    def is_ready(self) -> bool:
        """True if all checks are PASS or WARN (no FAIL)."""
        return not any(c.status == CheckStatus.FAIL for c in self.checks)

    def has_failures(self) -> bool:
        """True if any check is FAIL."""
        return any(c.status == CheckStatus.FAIL for c in self.checks)

    def has_warnings(self) -> bool:
        """True if any check is WARN (and no failures)."""
        return self.is_ready() and any(c.status == CheckStatus.WARN for c in self.checks)


async def check_spf(domain: str, provider: MailboxProvider) -> DNSCheck:
    """Check SPF record exists, is single, includes provider, and does not exceed
    10 DNS lookups (the hard limit).
    """
    try:
        records = await _dns_lookup(domain, dns.rdatatype.TXT)
    except (dns.exception.DNSException, OSError):
        return DNSCheck(
            record="SPF",
            status=CheckStatus.FAIL,
            found=None,
            expected="v=spf1 ...",
            fix=f"Add a TXT record at {domain} with value: v=spf1 include:_spf.google.com ~all (adjust for your provider)",
        )

    spf_records = [str(r) for r in records if str(r).startswith("v=spf1")]

    if not spf_records:
        return DNSCheck(
            record="SPF",
            status=CheckStatus.FAIL,
            found=None,
            expected="v=spf1 ...",
            fix=f"Add a TXT record at {domain} with value: v=spf1 include:_spf.google.com ~all (adjust for your provider)",
        )

    if len(spf_records) > 1:
        return DNSCheck(
            record="SPF",
            status=CheckStatus.FAIL,
            found=f"{len(spf_records)} records",
            expected="exactly 1",
            fix=f"Multiple SPF records found at {domain}. Consolidate into a single record using 'include:' directives instead.",
        )

    spf = spf_records[0]

    # Check for provider-specific includes
    provider_includes = {
        MailboxProvider.GOOGLE_WORKSPACE: "include:_spf.google.com",
        MailboxProvider.MICROSOFT_365: "include:spf.protection.outlook.com",
        MailboxProvider.GENERIC_SMTP: None,  # No standard check
    }

    expected_include = provider_includes.get(provider)
    if expected_include and expected_include not in spf:
        return DNSCheck(
            record="SPF",
            status=CheckStatus.WARN,
            found=spf,
            expected=expected_include,
            fix=f"Update SPF record at {domain} to include: {expected_include}",
        )

    # Count DNS lookups (simplified heuristic: count includes and other lookups)
    lookup_count = len([x for x in spf.split() if x.startswith(("include:", "a:", "mx:", "ptr:"))])
    if lookup_count > 10:
        return DNSCheck(
            record="SPF",
            status=CheckStatus.FAIL,
            found=f"{lookup_count} lookups",
            expected="≤10",
            fix=f"SPF record at {domain} exceeds the 10-lookup limit. Consolidate includes or use a third-party SPF flattening service.",
        )

    return DNSCheck(
        record="SPF",
        status=CheckStatus.PASS,
        found=spf,
        expected=None,
        fix="",
    )


async def check_dkim(domain: str, provider: MailboxProvider) -> DNSCheck:
    """Check DKIM public key record exists at the provider's selector."""
    selectors = DKIM_SELECTORS.get(provider, ["default"])

    for selector in selectors:
        dkim_domain = f"{selector}._domainkey.{domain}"
        try:
            records = await _dns_lookup(dkim_domain, dns.rdatatype.TXT)
            dkim_record = str(records[0]) if records else None
            if dkim_record and "v=DKIM1" in dkim_record:
                return DNSCheck(
                    record="DKIM",
                    status=CheckStatus.PASS,
                    found=f"Selector: {selector}",
                    expected=None,
                    fix="",
                )
        except (dns.exception.DNSException, OSError):
            continue

    selectors_str = ", ".join(selectors)
    return DNSCheck(
        record="DKIM",
        status=CheckStatus.FAIL,
        found=None,
        expected=f"TXT at {selectors_str}._domainkey.{domain}",
        fix=f"Add DKIM public key record at {selectors_str}._domainkey.{domain} via your mail provider's setup.",
    )


async def check_dmarc(domain: str) -> DNSCheck:
    """Check DMARC policy exists. p=none is a warning (weak), missing is a failure."""
    dmarc_domain = f"_dmarc.{domain}"
    try:
        records = await _dns_lookup(dmarc_domain, dns.rdatatype.TXT)
        dmarc_record = str(records[0]) if records else None
    except (dns.exception.DNSException, OSError):
        dmarc_record = None

    if not dmarc_record or "v=DMARC1" not in dmarc_record:
        return DNSCheck(
            record="DMARC",
            status=CheckStatus.FAIL,
            found=None,
            expected="v=DMARC1; p=...",
            fix=f"Add TXT record at {dmarc_domain} with value: v=DMARC1; p=none; rua=mailto:dmarc@{domain}",
        )

    if "p=none" in dmarc_record:
        return DNSCheck(
            record="DMARC",
            status=CheckStatus.WARN,
            found=dmarc_record,
            expected="p=quarantine or p=reject",
            fix=f"Update DMARC record at {dmarc_domain} to set p=quarantine when confident in SPF/DKIM alignment.",
        )

    return DNSCheck(
        record="DMARC",
        status=CheckStatus.PASS,
        found=dmarc_record,
        expected=None,
        fix="",
    )


async def check_ptr(ip_address: str) -> DNSCheck:
    """Check reverse DNS (PTR) for self-hosted SMTP. Optional; skipped if IP is None."""
    if not ip_address:
        return DNSCheck(
            record="PTR",
            status=CheckStatus.WARN,
            found=None,
            expected="Hostname matching sending domain",
            fix="Configure reverse DNS (PTR) for your sending IP if self-hosted. Not required for managed providers.",
        )

    try:
        # Reverse the IP octets and append .in-addr.arpa
        parts = ip_address.split(".")
        reverse_ip = ".".join(reversed(parts)) + ".in-addr.arpa"
        records = await _dns_lookup(reverse_ip, dns.rdatatype.PTR)
        hostname = str(records[0]).rstrip(".") if records else None
    except (dns.exception.DNSException, OSError):
        hostname = None

    if not hostname:
        return DNSCheck(
            record="PTR",
            status=CheckStatus.WARN,
            found=None,
            expected="Valid reverse DNS entry",
            fix=f"Contact your host to set PTR record for {ip_address} to match your sending domain.",
        )

    return DNSCheck(
        record="PTR",
        status=CheckStatus.PASS,
        found=hostname,
        expected=None,
        fix="",
    )


async def check_mx(domain: str) -> DNSCheck:
    """Check MX records exist."""
    try:
        records = await _dns_lookup(domain, dns.rdatatype.MX)
    except (dns.exception.DNSException, OSError):
        records = []

    if not records:
        return DNSCheck(
            record="MX",
            status=CheckStatus.FAIL,
            found=None,
            expected="MX records",
            fix=f"Ensure {domain} has MX records configured at your domain registrar.",
        )

    mx_hosts = ", ".join(str(r).split()[-1].rstrip(".") for r in records)
    return DNSCheck(
        record="MX",
        status=CheckStatus.PASS,
        found=mx_hosts,
        expected=None,
        fix="",
    )


async def check_alignment(
    domain: str,
    from_domain: str,
    provider: MailboxProvider,
) -> DNSCheck:
    """Check that the From domain aligns with SPF/DKIM domains."""
    if domain == from_domain:
        return DNSCheck(
            record="alignment",
            status=CheckStatus.PASS,
            found=from_domain,
            expected=None,
            fix="",
        )

    return DNSCheck(
        record="alignment",
        status=CheckStatus.FAIL,
        found=from_domain,
        expected=domain,
        fix=f"Use {domain} (your mailbox domain) as the From address, or configure SPF/DKIM for {from_domain}.",
    )


async def preflight(
    domain: str,
    from_domain: str | None,
    provider: MailboxProvider,
    ip_address: str | None = None,
) -> DNSPreflight:
    """Run all DNS checks and return a preflight result.

    Args:
        domain: The mailbox's sending domain (e.g. 'example.com')
        from_domain: The domain in the From header. If None, assumed same as domain.
        provider: The mailbox provider (GOOGLE_WORKSPACE, etc.)
        ip_address: IP address for PTR check (self-hosted only)

    Returns:
        DNSPreflight with results for all checks
    """
    if from_domain is None:
        from_domain = domain

    checks = [
        await check_spf(domain, provider),
        await check_dkim(domain, provider),
        await check_dmarc(domain),
        await check_mx(domain),
        await check_alignment(domain, from_domain, provider),
    ]

    if ip_address:
        checks.append(await check_ptr(ip_address))

    return DNSPreflight(checks=checks)


async def _dns_lookup(domain: str, rdtype: dns.rdatatype.RdataType | str) -> list[str]:
    """Async-compatible wrapper around dns.resolver.

    Since dnspython's resolver is synchronous, this just calls it directly.
    In production, this would use an async DNS client or run the sync call
    in a thread pool.
    """
    try:
        answer = dns.resolver.resolve(domain, rdtype, rdclass=IN)
        rrset = answer.rrset
        if rrset is None:
            return []
        return [str(rr) for rr in rrset]
    except (dns.exception.DNSException, OSError):
        return []


__all__ = [
    "CheckStatus",
    "DNSCheck",
    "DNSPreflight",
    "check_alignment",
    "check_dkim",
    "check_dmarc",
    "check_mx",
    "check_ptr",
    "check_spf",
    "preflight",
]
