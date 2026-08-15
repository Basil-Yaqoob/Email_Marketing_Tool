"""Person extraction: names, titles, and a role_class derived from the
title text. Generalises past the prototype's medical-only regexes.

Strategies, highest confidence first:

    JSON-LD Organization.founder / .employee            0.95
    Impressum "Geschäftsführer" / "Vertreten durch" /
      "Inhaber" / "Verantwortlich für den Inhalt"        0.90
    Title-adjacent block (name element, title sibling)   0.80
    Credential pattern ("Jane Smith, DDS")                0.70
    "Dr. Jane Smith"                                      0.65
    Byline / "Founded by" prose                           0.55

Reject aggressively: page section headings ("Our Team"), testimonial
authors, stock-photo captions. A wrong name is worse than no name — it
produces a confidently mis-addressed email.

Known limitation, not solved here: a real first name that is also a city
("Austin Smith" vs "Austin, TX") needs a gazetteer to disambiguate
reliably, which this module doesn't have. Comma-adjacent "Austin, TX"
patterns are rejected structurally (see _looks_like_name), but "Austin
Smith" standing alone would pass through — flagged, not silently assumed
solved.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from bs4 import BeautifulSoup, Tag

from app.db.models.enums import RoleClass

CONF_JSONLD = 0.95
CONF_IMPRESSUM = 0.90
CONF_HEADING_ADJACENT = 0.80
CONF_CREDENTIALED = 0.70
CONF_DR_PREFIX = 0.65
CONF_BYLINE = 0.55
MAX_CONFIDENCE = 0.99

# Section headings and generic labels that look like a name-shaped string
# but never are — the prototype's exact failure mode was treating "Our
# Team" as a person.
_SECTION_HEADING_PHRASES = frozenset(
    {
        "our team",
        "meet our team",
        "meet the team",
        "the team",
        "our story",
        "about us",
        "who we are",
        "leadership",
        "management",
        "our people",
        "meet the provider",
        "our staff",
        "what our customers say",
        "customer stories",
        "testimonials",
        "reviews",
        "our services",
        "get in touch",
        "contact us",
        "visit us",
        "co-founders",
        "executive",
    }
)

_CREDENTIALS = "DDS|MD|DO|DMD|DC|NP|PA|RN|DVM|PhD|CPA|Esq|JD|MBA"

_TITLE_KEYWORDS_OWNER = frozenset(
    {
        "owner",
        "founder",
        "co-founder",
        "president",
        "ceo",
        "chief executive",
        "managing director",
        "geschäftsführer",
        "geschaeftsfuehrer",
        "principal",
        "proprietor",
        "chairman",
        "executive chairman",
    }
)
_TITLE_KEYWORDS_MANAGER = frozenset(
    {
        "manager",
        "director",
        "head of",
        "lead",
        "supervisor",
        "coordinator",
        "chief operating",
        "coo",
        "chief financial",
        "cfo",
        "vp",
        "vice president",
    }
)
_TITLE_KEYWORDS_MARKETING = frozenset(
    {
        "marketing",
        "communications",
        "pr manager",
        "public relations",
        "brand",
        "growth",
        "social media",
    }
)

# One or two capitalised words, e.g. "Jane Smith" or "Sven Oliver Rüsche".
_NAME_SHAPE = r"[A-ZÀ-Ý][a-zà-ÿ'\-]+(?:\s+[A-ZÀ-Ý][a-zà-ÿ'\-]+){1,2}"
NAME_RE = re.compile(rf"^{_NAME_SHAPE}$")

# Job-title and role words that are capitalised just like a name ("Executive
# Chairman", "Software Engineer", "Support Team Lead") and would otherwise
# pass NAME_RE. Real team-page markup puts these directly adjacent to a
# real name (heading_adjacent's whole strategy), so the shape check alone
# can't tell them apart -- reject anything containing one of these words.
_TITLE_WORDS = frozenset(
    {
        "engineer",
        "engineering",
        "manager",
        "director",
        "lead",
        "specialist",
        "administrator",
        "coordinator",
        "designer",
        "developer",
        "analyst",
        "consultant",
        "executive",
        "chairman",
        "chairwoman",
        "chairperson",
        "president",
        "officer",
        "associate",
        "assistant",
        "representative",
        "agent",
        "advocate",
        "architect",
        "scientist",
        "accountant",
        "counsel",
        "controller",
        "team",
        "support",
        "success",
        "solutions",
        "operations",
        "sales",
        "finance",
        "product",
        "infrastructure",
        "founder",
        "cofounder",
        "co-founder",
        "content",
        "creator",
        "platform",
        "automation",
        "growth",
        "digital",
        "senior",
        "junior",
        "global",
        "regional",
    }
)

_IMPRESSUM_PATTERNS = [
    re.compile(rf"Geschäftsführer(?:in)?\s*:?\s*({_NAME_SHAPE})"),
    re.compile(rf"Vertreten durch\s*:?\s*({_NAME_SHAPE})"),
    re.compile(rf"Inhaber(?:in)?\s*:?\s*({_NAME_SHAPE})"),
    re.compile(rf"Verantwortlich für den Inhalt\s*:?\s*({_NAME_SHAPE})"),
]
CREDENTIALED_NAME_RE = re.compile(rf"({_NAME_SHAPE}),\s*(?:{_CREDENTIALS})\b")
DR_PREFIX_RE = re.compile(rf"\bDr\.\s+({_NAME_SHAPE})\b")
BYLINE_RE = re.compile(rf"(?:Founded by|Written by|Started by)\s+({_NAME_SHAPE})\b")

_HINT_TO_TITLE = {
    "founder": "Founder",
    "founders": "Founder",
    "employee": None,
    "employees": None,
}


@dataclass(frozen=True, slots=True)
class PersonHit:
    name: str
    title: str | None
    confidence: float
    strategy: str
    source_url: str
    role_class: RoleClass


def extract_people(html: str, url: str) -> list[PersonHit]:
    soup = BeautifulSoup(html, "lxml")

    hits: list[PersonHit] = []
    hits.extend(_from_jsonld(soup, url))
    hits.extend(_from_impressum(soup, url))
    hits.extend(_from_heading_adjacent(soup, url))
    hits.extend(_from_credentialed_name(soup, url))
    hits.extend(_from_dr_prefix(soup, url))
    hits.extend(_from_byline(soup, url))
    return _dedupe(hits)


def _looks_like_name(text: str) -> bool:
    cleaned = text.strip().rstrip(",.:")
    lowered = cleaned.lower()
    if lowered in _SECTION_HEADING_PHRASES:
        return False
    if not NAME_RE.match(cleaned):
        return False
    words = set(re.findall(r"[a-zà-ÿ\-]+", lowered))
    return not words & _TITLE_WORDS


def _role_class_from_title(title: str | None) -> RoleClass:
    if not title:
        return RoleClass.OTHER
    lowered = title.lower()
    if any(kw in lowered for kw in _TITLE_KEYWORDS_OWNER):
        return RoleClass.OWNER
    if any(kw in lowered for kw in _TITLE_KEYWORDS_MARKETING):
        return RoleClass.MARKETING
    if any(kw in lowered for kw in _TITLE_KEYWORDS_MANAGER):
        return RoleClass.MANAGER
    return RoleClass.OTHER


def _make_hit(
    name: str, title: str | None, confidence: float, strategy: str, url: str
) -> PersonHit:
    return PersonHit(
        name=name,
        title=title,
        confidence=min(confidence, MAX_CONFIDENCE),
        strategy=strategy,
        source_url=url,
        role_class=_role_class_from_title(title),
    )


def _from_jsonld(soup: BeautifulSoup, url: str) -> list[PersonHit]:
    hits: list[PersonHit] = []
    for script in soup.find_all("script", type="application/ld+json"):
        text = script.string or script.get_text()
        if not text:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        for person, hint in _walk_jsonld_for_people(data):
            name = person.get("name")
            if not isinstance(name, str) or not _looks_like_name(name):
                continue
            job_title = person.get("jobTitle")
            title = job_title if isinstance(job_title, str) else _HINT_TO_TITLE.get(hint or "")
            hits.append(_make_hit(name.strip(), title, CONF_JSONLD, "jsonld", url))
    return hits


def _walk_jsonld_for_people(
    node: object, key_hint: str | None = None
) -> list[tuple[dict[str, Any], str | None]]:
    found: list[tuple[dict[str, Any], str | None]] = []
    if isinstance(node, dict):
        if node.get("@type") == "Person":
            found.append((node, key_hint))
        for key, value in node.items():
            hint = key if key in _HINT_TO_TITLE else key_hint
            found.extend(_walk_jsonld_for_people(value, hint))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk_jsonld_for_people(item, key_hint))
    return found


def _from_impressum(soup: BeautifulSoup, url: str) -> list[PersonHit]:
    # Block-scoped, not whole-page-flattened: real Impressum pages put the
    # name and the following street address on separate lines (<br>
    # separated), and matching against the whole flattened text lets a
    # greedy name-capture run straight into the address. See _text_blocks.
    hits: list[PersonHit] = []
    for block in _text_blocks(soup):
        for pattern in _IMPRESSUM_PATTERNS:
            match = pattern.search(block)
            if match is None:
                continue
            candidate = match.group(1).strip()
            if not _looks_like_name(candidate):
                continue
            hits.append(_make_hit(candidate, "Geschäftsführer", CONF_IMPRESSUM, "impressum", url))
    return hits


def _has_element_children(tag: Tag) -> bool:
    return any(isinstance(child, Tag) for child in tag.children)


def _find_adjacent_title(tag: Tag, *, max_siblings: int = 3) -> str | None:
    sibling = tag.find_next_sibling()
    checked = 0
    while sibling is not None and checked < max_siblings:
        if isinstance(sibling, Tag):
            text = sibling.get_text(strip=True)
            if text and len(text) < 80 and not _looks_like_name(text):
                return text
        sibling = sibling.find_next_sibling() if isinstance(sibling, Tag) else None
        checked += 1
    return None


def _from_heading_adjacent(soup: BeautifulSoup, url: str) -> list[PersonHit]:
    """A name-shaped leaf element (no nested tags of its own — otherwise
    its text would be the whole card, name and bio concatenated) followed
    by a short sibling that isn't itself name-shaped. Matches both
    `<h3>Name</h3><p>Title</p>` and real-world div/div sibling pairs
    (e.g. a `member__details--name` / `member__details--title` pair).
    """
    hits: list[PersonHit] = []
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "div", "span", "p", "dt"]):
        if _has_element_children(tag):
            continue
        text = tag.get_text(strip=True)
        if not _looks_like_name(text):
            continue
        title = _find_adjacent_title(tag)
        if title is None:
            continue
        hits.append(_make_hit(text, title, CONF_HEADING_ADJACENT, "heading_adjacent", url))
    return hits


def _text_blocks(soup: BeautifulSoup) -> list[str]:
    """Block-scoped text chunks (roughly one per paragraph/heading/<br>
    line), not the whole page flattened to one string. A heading like
    "Meet the Provider" sits immediately before a name in real markup
    often enough that matching across the whole flattened text lets a
    greedy capitalised-word capture run straight from the heading into
    the name (or, in the Impressum case, from the name into the
    following address line). Splitting on block boundaries first avoids
    both.
    """
    return [line.strip() for line in soup.get_text("\n").split("\n") if line.strip()]


def _from_credentialed_name(soup: BeautifulSoup, url: str) -> list[PersonHit]:
    hits: list[PersonHit] = []
    for block in _text_blocks(soup):
        for match in CREDENTIALED_NAME_RE.finditer(block):
            name = match.group(1).strip()
            if not _looks_like_name(name):
                continue
            credential = block[match.end(1) : match.end()].strip(", ").strip()
            hits.append(_make_hit(name, credential or None, CONF_CREDENTIALED, "credentialed", url))
    return hits


def _from_dr_prefix(soup: BeautifulSoup, url: str) -> list[PersonHit]:
    hits: list[PersonHit] = []
    for block in _text_blocks(soup):
        for match in DR_PREFIX_RE.finditer(block):
            name = match.group(1).strip()
            if not _looks_like_name(name):
                continue
            hits.append(_make_hit(name, None, CONF_DR_PREFIX, "dr_prefix", url))
    return hits


def _from_byline(soup: BeautifulSoup, url: str) -> list[PersonHit]:
    hits: list[PersonHit] = []
    for block in _text_blocks(soup):
        for match in BYLINE_RE.finditer(block):
            name = match.group(1).strip()
            if not _looks_like_name(name):
                continue
            hits.append(_make_hit(name, None, CONF_BYLINE, "byline", url))
    return hits


def _dedupe(hits: list[PersonHit]) -> list[PersonHit]:
    best: dict[str, PersonHit] = {}
    for hit in hits:
        key = hit.name.lower()
        current = best.get(key)
        if current is None or hit.confidence > current.confidence:
            best[key] = hit
    return sorted(best.values(), key=lambda h: h.confidence, reverse=True)
