"""Email extraction: every strategy the prototype had, plus JSON-LD,
mailto and obfuscation handling it lacked.

Strategies, highest confidence first:

    JSON-LD `email` field              0.95   structured, unambiguous
    `mailto:` link                     0.90   explicit intent
    Plain text regex                   0.75   the prototype's only method
    HTML-entity encoded                0.70   &#106;&#111;... decoded
    `[at]`/`(at)`/` at ` obfuscation    0.70   normalise then validate
    + footer / contact-block proximity +0.05  positional bonus

BAD_HINTS and ROLE_PREFIXES are ported from the prototype's config.py —
both were well chosen and are kept close to verbatim.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html import unescape

from bs4 import BeautifulSoup, Tag

CONF_JSONLD = 0.95
CONF_MAILTO = 0.90
CONF_PLAIN_TEXT = 0.75
CONF_HTML_ENTITY = 0.70
CONF_OBFUSCATED = 0.70
PROXIMITY_BONUS = 0.05
MAX_CONFIDENCE = 0.99

# Ported near-verbatim from the prototype. Extended with common CDN and
# analytics noise a naive email regex snags on real pages.
BAD_HINTS = frozenset(
    {
        "sentry",
        "sentry.io",
        "wixpress",
        "wix.com",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".svg",
        ".webp",
        "@2x",
        "@3x",
        "example.com",
        "yourdomain",
        "yoursite",
        "domain.com",
        "email.com",
        "placeholder",
        "godaddy",
        "cloudflare",
        "cloudfront",
        "google-analytics",
        "googletagmanager",
        "gravatar.com",
        "schema.org",
        "w3.org",
    }
)

ROLE_PREFIXES = frozenset(
    {
        "info",
        "contact",
        "sales",
        "support",
        "admin",
        "office",
        "hello",
        "team",
        "help",
        "service",
        "billing",
        "careers",
        "jobs",
        "press",
        "media",
        "marketing",
        "webmaster",
        "noreply",
        "no-reply",
        "enquiries",
        "inquiries",
        "reception",
        "frontdesk",
        "general",
        "mail",
    }
)

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

_AT = r"[\[\(]\s*at\s*[\]\)]"
_DOT = r"[\[\(]\s*dot\s*[\]\)]"

# "name [at] domain [dot] com" -- both @ and . obfuscated.
FULLY_OBFUSCATED_RE = re.compile(
    rf"([a-zA-Z0-9._%+\-]+)\s*{_AT}\s*([a-zA-Z0-9.\-]+)\s*{_DOT}\s*([a-zA-Z]{{2,}})",
    re.IGNORECASE,
)
# "name(at)domain.com" -- only @ obfuscated, dot left literal.
AT_ONLY_OBFUSCATED_RE = re.compile(
    rf"([a-zA-Z0-9._%+\-]+)\s*{_AT}\s*([a-zA-Z0-9.\-]+\.[a-zA-Z]{{2,}})",
    re.IGNORECASE,
)
# "name at domain dot com" -- bare words, no brackets at all.
WORD_AT_RE = re.compile(
    r"([a-zA-Z0-9._%+\-]+)\s+at\s+([a-zA-Z0-9.\-]+)\s+dot\s+([a-zA-Z]{2,})",
    re.IGNORECASE,
)
# A run of numeric character references long enough to plausibly decode
# to an email address (e.g. &#106;&#111;&#104;&#110;&#64;...).
HTML_ENTITY_RUN_RE = re.compile(r"(?:&#\d{1,4};){8,}")


@dataclass(frozen=True, slots=True)
class EmailHit:
    value: str
    confidence: float
    strategy: str
    source_url: str
    is_role_account: bool


def extract_emails(html: str, base_url: str) -> list[EmailHit]:
    """Every strategy, each tagged with how it was found (feeds confidence).

    Runs every strategy rather than stopping at the first hit — the
    caller (app/resolvers/company/website.py) is what decides, across all
    fetched pages, which candidate wins; this function's job is only to
    surface everything found on one page.
    """
    soup = BeautifulSoup(html, "lxml")

    hits: list[EmailHit] = []
    hits.extend(_from_jsonld(soup, base_url))
    hits.extend(_from_mailto(soup, base_url))
    hits.extend(_from_html_entities(html, base_url))
    hits.extend(_from_plain_text(soup, base_url))
    hits.extend(_from_obfuscation(soup, base_url))
    return _dedupe(hits)


def _is_role_account(local_part: str) -> bool:
    return local_part.lower() in ROLE_PREFIXES


def _is_bad(address: str) -> bool:
    lowered = address.lower()
    return any(hint in lowered for hint in BAD_HINTS)


def _attr_str(tag: Tag, name: str) -> str:
    """A tag attribute (e.g. class) may come back as a single string or a
    list of strings depending on the attribute and parser -- normalise to
    one string either way.
    """
    value = tag.get(name, "")
    if isinstance(value, list):
        return " ".join(value)
    return value or ""


def _near_contact_block(tag: Tag | None, *, max_depth: int = 4) -> bool:
    """+0.05 positional bonus: an address inside <footer> or an element
    whose class/id mentions "contact" is more likely to be the business's
    real contact address than one caught incidentally elsewhere.
    """
    node = tag
    depth = 0
    while node is not None and depth < max_depth:
        if getattr(node, "name", None) == "footer":
            return True
        if isinstance(node, Tag):
            classes = _attr_str(node, "class")
            node_id = _attr_str(node, "id")
            haystack = f"{classes} {node_id}".lower()
            if "contact" in haystack or "footer" in haystack:
                return True
        node = node.parent
        depth += 1
    return False


def _make_hit(address: str, confidence: float, strategy: str, base_url: str) -> EmailHit:
    return EmailHit(
        value=address.lower(),
        confidence=min(confidence, MAX_CONFIDENCE),
        strategy=strategy,
        source_url=base_url,
        is_role_account=_is_role_account(address.split("@")[0]),
    )


def _from_jsonld(soup: BeautifulSoup, base_url: str) -> list[EmailHit]:
    hits: list[EmailHit] = []
    for script in soup.find_all("script", type="application/ld+json"):
        text = script.string or script.get_text()
        if not text:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        for email in _walk_jsonld_for_emails(data):
            if not EMAIL_RE.fullmatch(email) or _is_bad(email):
                continue
            hits.append(_make_hit(email, CONF_JSONLD, "jsonld", base_url))
    return hits


def _walk_jsonld_for_emails(node: object) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "email" and isinstance(value, str):
                found.append(value)
            else:
                found.extend(_walk_jsonld_for_emails(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk_jsonld_for_emails(item))
    return found


def _from_mailto(soup: BeautifulSoup, base_url: str) -> list[EmailHit]:
    hits: list[EmailHit] = []
    for link in soup.find_all("a", href=True):
        href = _attr_str(link, "href")
        if not href.lower().startswith("mailto:"):
            continue
        address = href[len("mailto:") :].split("?")[0].strip()
        if not EMAIL_RE.fullmatch(address) or _is_bad(address):
            continue
        confidence = CONF_MAILTO
        if _near_contact_block(link):
            confidence += PROXIMITY_BONUS
        hits.append(_make_hit(address, confidence, "mailto", base_url))
    return hits


def _from_html_entities(html: str, base_url: str) -> list[EmailHit]:
    """Operates on the raw HTML source, not the parsed tree — a naive
    scraper that regexes raw bytes (the prototype's approach) never sees
    an address hidden this way, since only a real parser decodes entity
    references. We decode with the stdlib rather than reimplementing it.
    """
    hits: list[EmailHit] = []
    for match in HTML_ENTITY_RUN_RE.finditer(html):
        decoded = unescape(match.group(0))
        email_match = EMAIL_RE.search(decoded)
        if email_match is None:
            continue
        address = email_match.group(0)
        if _is_bad(address):
            continue
        hits.append(_make_hit(address, CONF_HTML_ENTITY, "html_entity", base_url))
    return hits


def _from_plain_text(soup: BeautifulSoup, base_url: str) -> list[EmailHit]:
    hits: list[EmailHit] = []
    for node in soup.find_all(string=EMAIL_RE):
        for match in EMAIL_RE.finditer(str(node)):
            address = match.group(0)
            if _is_bad(address):
                continue
            confidence = CONF_PLAIN_TEXT
            if _near_contact_block(node.parent):
                confidence += PROXIMITY_BONUS
            hits.append(_make_hit(address, confidence, "plain_text", base_url))
    return hits


def _from_obfuscation(soup: BeautifulSoup, base_url: str) -> list[EmailHit]:
    hits: list[EmailHit] = []
    text = soup.get_text(" ")
    for pattern in (FULLY_OBFUSCATED_RE, WORD_AT_RE):
        for match in pattern.finditer(text):
            local, domain, tld = match.group(1), match.group(2), match.group(3)
            address = f"{local}@{domain}.{tld}"
            if not EMAIL_RE.fullmatch(address) or _is_bad(address):
                continue
            hits.append(_make_hit(address, CONF_OBFUSCATED, "obfuscated", base_url))
    for match in AT_ONLY_OBFUSCATED_RE.finditer(text):
        local, domain_and_tld = match.group(1), match.group(2)
        address = f"{local}@{domain_and_tld}"
        if not EMAIL_RE.fullmatch(address) or _is_bad(address):
            continue
        hits.append(_make_hit(address, CONF_OBFUSCATED, "obfuscated", base_url))
    return hits


def _dedupe(hits: list[EmailHit]) -> list[EmailHit]:
    """The same address found by two strategies on one page isn't two
    pieces of evidence, it's one fact discovered two ways -- keep the
    higher-confidence hit, don't stack them (that's Session 03's
    independence rule, applied within a single page).
    """
    best: dict[str, EmailHit] = {}
    for hit in hits:
        current = best.get(hit.value)
        if current is None or hit.confidence > current.confidence:
            best[hit.value] = hit
    return sorted(best.values(), key=lambda h: h.confidence, reverse=True)
