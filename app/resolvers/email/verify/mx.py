"""Rung 2 — MX lookup and provider detection.

Two outputs, both load-bearing:

  has_mx   — a domain with no MX records accepts no mail at all, so every
             address there is INVALID without touching SMTP.
  provider — decides whether rung 3 can produce information or only false
             confidence.

Provider detection is the honest-limits rung. Google Workspace and
Microsoft 365 answer RCPT TO with 250 for mailboxes that do not exist and
bounce afterwards, so probing them cannot distinguish a real address from
a typo. Both resolve to UNKNOWN here, without a probe — which is both more
truthful and cheaper than asking and believing the answer.

The resolver is a Protocol so tests never touch DNS. CLAUDE.md's testing
rule is that network is always mocked; a Protocol makes that structural
rather than a monkeypatch.
"""

from __future__ import annotations

from typing import Protocol

from app.core.errors import VerificationError
from app.resolvers.email.verify.models import MailProvider, MXResult

# Matched as suffixes against the lowercased MX host. Ordered most
# specific first where families overlap.
GOOGLE_MX_SUFFIXES = (
    "aspmx.l.google.com",
    "googlemail.com",
    "psmtp.com",
    ".google.com",
)
MICROSOFT_MX_SUFFIXES = (
    "mail.protection.outlook.com",
    "mail.eo.outlook.com",
    ".outlook.com",
    ".hotmail.com",
)
PROOFPOINT_MX_SUFFIXES = (
    "pphosted.com",
    "ppe-hosted.com",
)


class DnsResolver(Protocol):
    async def resolve_mx(self, domain: str) -> list[str]: ...


class DnsPythonResolver:
    """Real DNS via dnspython's async resolver.

    NXDOMAIN and "no MX records" are *answers*, not failures — both mean
    the domain accepts no mail, and both come back as an empty list. Only
    a genuine resolution failure (timeout, servfail, no nameservers
    reachable) raises VerificationError, because that is our side being
    broken rather than the domain being empty.
    """

    def __init__(self, *, timeout: float = 5.0) -> None:
        self._timeout = timeout

    async def resolve_mx(self, domain: str) -> list[str]:
        import dns.asyncresolver
        import dns.exception
        import dns.resolver

        resolver = dns.asyncresolver.Resolver()
        resolver.timeout = self._timeout
        resolver.lifetime = self._timeout

        try:
            answer = await resolver.resolve(domain, "MX")
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            return []
        except dns.exception.DNSException as exc:
            raise VerificationError(f"MX lookup failed for {domain}: {exc}") from exc

        hosts = [str(record.exchange).rstrip(".").lower() for record in answer]
        return [host for host in hosts if host]


def _matches(host: str, suffixes: tuple[str, ...]) -> bool:
    return any(host == suffix.lstrip(".") or host.endswith(suffix) for suffix in suffixes)


def detect_provider(hosts: list[str] | tuple[str, ...]) -> MailProvider:
    if not hosts:
        return MailProvider.NONE

    normalised = [host.strip().rstrip(".").lower() for host in hosts]
    if any(_matches(host, GOOGLE_MX_SUFFIXES) for host in normalised):
        return MailProvider.GOOGLE
    if any(_matches(host, MICROSOFT_MX_SUFFIXES) for host in normalised):
        return MailProvider.MICROSOFT
    if any(_matches(host, PROOFPOINT_MX_SUFFIXES) for host in normalised):
        return MailProvider.PROOFPOINT
    return MailProvider.OTHER


async def lookup_mx(domain: str, resolver: DnsResolver) -> MXResult:
    hosts = await resolver.resolve_mx(domain)
    normalised = tuple(host.strip().rstrip(".").lower() for host in hosts if host.strip())
    return MXResult(
        has_mx=bool(normalised),
        hosts=normalised,
        provider=detect_provider(normalised),
    )
