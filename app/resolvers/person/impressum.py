"""ImpressumResolver — dedicated German/Austrian/Swiss managing-director
extraction, split out from Session 06's generic crawler.

Session 06's WebsitePersonResolver already catches Geschäftsführer-style
text as one strategy among several (JSON-LD, heading-adjacent, byline,
...). This module exists for what that general-purpose strategy
deliberately doesn't do: extract the Handelsregister (commercial register)
number and registered address too, both useful for cross-referencing
against Session 07's registry resolvers, and treat the whole page as
what it legally is — a mandatory disclosure, not a scraped guess.

Reuses the exact same four regex patterns and name-shape validation as
Session 06's crawler (imported, not re-implemented) so the two modules
can never silently disagree about what counts as a valid Geschäftsführer
match. Confidence is 0.90 across the board: an Impressum is legally
required to be accurate under German TMG §5 — this is about as close to
ground truth as unverified web text gets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from bs4 import BeautifulSoup

from app.db.models.enums import RoleClass
from app.net.browser import BrowserFetcher
from app.net.client import HttpClient
from app.resolvers.base import BaseResolver, Candidate, LeadContext, Tier
from app.resolvers.company.extract_person import _IMPRESSUM_PATTERNS, _looks_like_name, _text_blocks
from app.resolvers.company.website import _fetch_pages

CONF_IMPRESSUM = 0.90

# Parallel to _IMPRESSUM_PATTERNS (imported above) -- same order, same
# count, enforced by strict=True in extract_impressum(). "Verantwortlich
# für den Inhalt" (responsible for page content, required under the
# Rundfunkstaatsvertrag) is often a marketing/comms role, not necessarily
# an owner, so it maps to OTHER rather than OWNER like the other three.
_PATTERN_LABELS: tuple[tuple[str, RoleClass], ...] = (
    ("Geschäftsführer", RoleClass.OWNER),
    ("Vertreten durch", RoleClass.OWNER),
    ("Inhaber", RoleClass.OWNER),
    ("Verantwortlich für den Inhalt", RoleClass.OTHER),
)

_HANDELSREGISTER_RE = re.compile(r"\b(HR[AB]\s*\d+)\b", re.IGNORECASE)
_REGISTER_COURT_RE = re.compile(r"Amtsgericht\s+([A-ZÀ-Ýa-zà-ÿ\-]+)")

_STREET_LINE_RE = re.compile(r"^([A-ZÀ-Ý][A-Za-zà-ÿ.\-]*(?:\s+[A-Za-zà-ÿ.\-]+)*\s+\d+\s*[a-z]?)$")
_POSTAL_LINE_RE = re.compile(r"^(\d{4,5})\s+([A-ZÀ-Ýa-zà-ÿ\-\s]+)$")


@dataclass(frozen=True, slots=True)
class ImpressumHit:
    name: str
    title: str
    role_class: RoleClass
    confidence: float
    source_url: str
    handelsregister_number: str | None
    registered_address: str | None


def _extract_handelsregister(text: str) -> str | None:
    number_match = _HANDELSREGISTER_RE.search(text)
    if number_match is None:
        return None
    number = re.sub(r"\s+", " ", number_match.group(1).upper()).strip()
    court_match = _REGISTER_COURT_RE.search(text)
    if court_match is not None:
        return f"Amtsgericht {court_match.group(1)}, {number}"
    return number


def _extract_address(blocks: list[str]) -> str | None:
    """A German business address is almost always two adjacent lines:
    "Musterstraße 12" then "12345 Musterstadt" — matched line-by-line
    against _text_blocks rather than the whole flattened page, same
    reasoning as Session 06's name-vs-address bleed fix.
    """
    for i, block in enumerate(blocks):
        street_match = _STREET_LINE_RE.match(block)
        if street_match is None or i + 1 >= len(blocks):
            continue
        postal_match = _POSTAL_LINE_RE.match(blocks[i + 1])
        if postal_match is not None:
            return f"{street_match.group(1)}, {blocks[i + 1]}"
    return None


def _dedupe(hits: list[ImpressumHit]) -> list[ImpressumHit]:
    best: dict[str, ImpressumHit] = {}
    for hit in hits:
        key = hit.name.lower()
        if key not in best:
            best[key] = hit
    return list(best.values())


def extract_impressum(html: str, url: str) -> list[ImpressumHit]:
    soup = BeautifulSoup(html, "lxml")
    blocks = _text_blocks(soup)
    handelsregister = _extract_handelsregister("\n".join(blocks))
    address = _extract_address(blocks)

    hits: list[ImpressumHit] = []
    for block in blocks:
        for pattern, (label, role_class) in zip(_IMPRESSUM_PATTERNS, _PATTERN_LABELS, strict=True):
            match = pattern.search(block)
            if match is None:
                continue
            candidate = match.group(1).strip()
            if not _looks_like_name(candidate):
                continue
            hits.append(
                ImpressumHit(
                    name=candidate,
                    title=label,
                    role_class=role_class,
                    confidence=CONF_IMPRESSUM,
                    source_url=url,
                    handelsregister_number=handelsregister,
                    registered_address=address,
                )
            )
    return _dedupe(hits)


class ImpressumResolver(BaseResolver):
    name = "impressum"
    field = "person_name"
    tier = Tier.FREE
    cost_per_call = Decimal("0")
    jurisdictions: frozenset[str] | None = frozenset({"DE", "AT", "CH"})

    def __init__(self, *, http: HttpClient, browser: BrowserFetcher | None = None) -> None:
        self._http = http
        self._browser = browser

    async def resolve(self, ctx: LeadContext) -> list[Candidate]:
        # Reuses Session 06's page-fetching exactly -- same crawl paths
        # (which already include every DACH Impressum path variant), same
        # SPA escalation, same cache. This resolver differs only in what
        # it extracts from the pages once fetched.
        pages = await _fetch_pages(ctx, self._http, self._browser)

        candidates: list[Candidate] = []
        for url, html in pages:
            for hit in extract_impressum(html, url):
                candidates.append(
                    Candidate(
                        value=hit.name,
                        confidence=hit.confidence,
                        source=self.name,
                        source_url=url,
                        extra={
                            "title": hit.title,
                            "role_class": hit.role_class.value,
                            "handelsregister_number": hit.handelsregister_number,
                            "registered_address": hit.registered_address,
                        },
                    )
                )
        return candidates
