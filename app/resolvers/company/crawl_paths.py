"""Multi-language crawl paths, ordered by expected value.

The prototype tried 11 English-only paths. This list keeps those (they
still work) and adds the paths that actually move coverage:

- German/Austrian/Swiss Impressum-family paths are **legally mandated** —
  every commercial site in DACH must publish one naming a managing
  director. That single path family carries the highest yield-per-fetch
  of anything here.
- French and Spanish/Italian equivalents extend the same idea past
  English-only markets.

Collect from every fetched page, then rank — never stop at the first hit.
A generic `info@` on the homepage must not suppress an owner's address
found later on `/team` (see app/resolvers/company/website.py).
"""

from __future__ import annotations

import xml.etree.ElementTree as ElementTree
from collections.abc import Sequence

CRAWL_PATHS: list[str] = [
    "",  # homepage
    # English
    "contact",
    "contact-us",
    "about",
    "about-us",
    "team",
    "our-team",
    "meet-the-team",
    "staff",
    "people",
    "leadership",
    "management",
    "doctors",
    "providers",
    "our-story",
    # German / Austrian / Swiss — legally mandated, highest yield in DACH
    "impressum",
    "kontakt",
    "ueber-uns",
    "team",
    "unternehmen",
    # French
    "mentions-legales",
    "a-propos",
    "equipe",
    "contact",
    # Spanish / Italian
    "quienes-somos",
    "equipo",
    "contacto",
    "chi-siamo",
    "contatti",
]

DEFAULT_MAX_FETCHES_PER_SITE = 12

_SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


def parse_sitemap(xml_text: str) -> list[str]:
    """Extract every <loc> URL from a sitemap.xml body.

    Malformed XML yields an empty list rather than raising — a broken
    sitemap degrades to blind path-guessing, it doesn't crash the crawl.
    """
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return []

    locs = [el.text.strip() for el in root.findall(".//sm:loc", _SITEMAP_NS) if el.text]
    if not locs:
        # Some sitemaps omit the namespace declaration despite the schema
        # requiring it — fall back to a namespace-less search.
        locs = [el.text.strip() for el in root.findall(".//loc") if el.text]
    return locs


def select_crawl_urls(
    base_url: str,
    *,
    sitemap_urls: Sequence[str] | None = None,
    max_fetches: int = DEFAULT_MAX_FETCHES_PER_SITE,
) -> list[str]:
    """The ordered list of URLs to fetch for one site, capped at
    max_fetches.

    When a sitemap was parsed, URLs it actually lists are tried first
    (fewer 404s) while keeping the high-value-first ordering within each
    group; paths absent from the sitemap are still tried afterward, since
    a missing sitemap entry doesn't mean a missing page.
    """
    base = base_url.rstrip("/")

    seen: set[str] = set()
    ordered: list[str] = []
    for path in CRAWL_PATHS:
        candidate = f"{base}/{path}".rstrip("/") if path else base
        if candidate not in seen:
            seen.add(candidate)
            ordered.append(candidate)

    if not sitemap_urls:
        return ordered[:max_fetches]

    sitemap_set = {u.rstrip("/") for u in sitemap_urls}
    in_sitemap = [u for u in ordered if u in sitemap_set]
    not_in_sitemap = [u for u in ordered if u not in sitemap_set]
    return (in_sitemap + not_in_sitemap)[:max_fetches]
