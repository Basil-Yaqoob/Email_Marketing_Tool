"""Rung 1 — syntax, disposable domains, role prefixes.

Free, instant, entirely local. Its job is to settle the addresses that
never deserved a network round trip, so rungs 2 and 3 only see plausible
candidates.

Deliberately stricter than app/resolvers/company/extract_email.py's
EMAIL_RE. That regex runs over messy page text where being permissive is
correct (better to over-extract and filter later). Here the input is a
specific claimed address and being permissive means sending mail into the
void, so the checks are the RFC-shaped ones a mail server would apply.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.db.models.enums import VerifyStatus
from app.resolvers.company.extract_email import ROLE_PREFIXES

_DISPOSABLE_FILE = Path(__file__).with_name("disposable_domains.txt")

# Local part: dot-separated atoms, no leading/trailing/doubled dots.
# Domain: dot-separated labels, no leading/trailing hyphen, TLD >= 2 alpha.
_LOCAL_ATOM = r"[a-zA-Z0-9!#$%&'*+/=?^_`{|}~-]+"
_LABEL = r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
EMAIL_SYNTAX_RE = re.compile(
    rf"^{_LOCAL_ATOM}(?:\.{_LOCAL_ATOM})*@{_LABEL}(?:\.{_LABEL})*\.[a-zA-Z]{{2,}}$"
)

# RFC 5321: 64 octets local part, 254 total for the reverse-path.
MAX_LOCAL_PART = 64
MAX_ADDRESS_LENGTH = 254


def _load_disposable_domains() -> frozenset[str]:
    if not _DISPOSABLE_FILE.exists():  # pragma: no cover - bundled with the package
        return frozenset()
    lines = _DISPOSABLE_FILE.read_text(encoding="utf-8").splitlines()
    return frozenset(
        line.strip().lower() for line in lines if line.strip() and not line.startswith("#")
    )


DISPOSABLE_DOMAINS = _load_disposable_domains()


def split_address(address: str) -> tuple[str, str]:
    """(local_part, domain), both lowercased. Returns ("", "") when the
    address has no single unambiguous @.
    """
    if address.count("@") != 1:
        return "", ""
    local, _, domain = address.strip().lower().partition("@")
    return local, domain


def is_valid_syntax(address: str) -> bool:
    candidate = address.strip()
    if len(candidate) > MAX_ADDRESS_LENGTH:
        return False
    local, _ = split_address(candidate)
    if not local or len(local) > MAX_LOCAL_PART:
        return False
    return EMAIL_SYNTAX_RE.match(candidate) is not None


def is_disposable(domain: str) -> bool:
    return domain.lower() in DISPOSABLE_DOMAINS


def is_role_address(local_part: str) -> bool:
    return local_part.lower() in ROLE_PREFIXES


def check_syntax(address: str) -> VerifyStatus | None:
    """A settled status, or None meaning "plausible — continue to rung 2".

    ROLE is a settled *and* legitimate outcome: info@ is usually a real,
    deliverable mailbox, it just isn't a person. Sending policy treats it
    differently rather than discarding it, so it stops here rather than
    burning a probe on a mailbox we already know exists.
    """
    if not is_valid_syntax(address):
        return VerifyStatus.INVALID

    local, domain = split_address(address)
    if is_disposable(domain):
        return VerifyStatus.INVALID
    if is_role_address(local):
        return VerifyStatus.ROLE
    return None
