"""Research tools — ported from recent_news_agent/channels.py onto Session
04's HTTP layer and Session 08's pluggable search.

Design note preserved from the original: these are *batch* operations,
not micro-tools. `scan_website` and `scan_social` each run several
network calls and return one consolidated blob, so a cheap fast model
doesn't have to orchestrate a dozen round trips itself — the Python does
the fan-out, the model does the judging. That split is why the measured
cost is ~$0.03/lead, and it must survive the port unchanged.

Three deliberate divergences from the original, all forced by decisions
already made and tested in prior sessions, not new judgement calls here:

1. **No direct Instagram JSON probe.** The original made "one
   opportunistic direct attempt" at Instagram's web API alongside the
   search-index lookup. Session 08 built and tested a hard architectural
   guard (tests/unit/test_no_linkedin_fetch.py) that no code path may
   ever request instagram.com — `scan_social` here is search-index only,
   which is also exactly what the session plan itself describes.
2. **Link discovery reads raw HTML, not stripped text.** The original's
   `_discover_links` regex-scanned `fetch()`'s already-HTML-stripped
   output, where `<a href>` attributes have already been destroyed by the
   tag stripper — so in practice it rarely found anything, and the
   website scan leaned on its hardcoded NEWS_PATHS guesses. Session 06
   already gives us real, tested `<a href>` parsing via BeautifulSoup;
   using it here is fixing an accidental limitation with infrastructure
   we already trust, not a judgement change to what counts as a hook.
3. **No r.jina.ai reader-proxy fallback.** The original's flaky-DNS
   workaround was proven against its own datacenter IP; HttpClient's
   built-in retry/backoff (Session 04) is the equivalent resilience layer
   here, and adding an unproven third-party dependency isn't warranted
   without the same evidence.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.core.errors import RobotsDisallowedError, UpstreamError
from app.llm.types import Tool
from app.net.client import HttpClient
from app.net.search import SearchBackend, SearchResult

# Pages worth checking on a company site, in rough order of news density.
NEWS_PATHS = (
    "/blog", "/news", "/about", "/about-us", "/team", "/our-team",
    "/locations", "/specials", "/offers", "/press", "/careers",
)  # fmt: skip

_LINK_KEYWORDS = (
    "blog", "news", "about", "team", "location", "special",
    "offer", "press", "award", "career", "updates",
)  # fmt: skip

MAX_PAGE_CHARS = 12_000
MAX_SEARCH_RESULTS = 4
DEFAULT_MAX_PAGES = 5


@dataclass(frozen=True, slots=True)
class ToolContext:
    """What every tool needs, bound once per research call rather than
    threaded through every function signature.
    """

    http: HttpClient
    search: SearchBackend


def _origin(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text("\n")
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _discover_links(html: str, base: str) -> list[str]:
    """Same-origin links whose path looks like a news-bearing page,
    pulled from real `<a href>` attributes -- see this module's
    docstring, point 2, on why this reads raw HTML rather than stripped
    text.
    """
    soup = BeautifulSoup(html, "lxml")
    found: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href")
        if not isinstance(href, str) or not href:
            continue
        url = urljoin(base, href)
        if not url.startswith(base):
            continue
        tail = url[len(base) :].lower()
        if tail.count("/") > 3:
            continue
        if any(keyword in tail for keyword in _LINK_KEYWORDS):
            found.append(url)
    return list(dict.fromkeys(found))


async def _fetch_text(ctx: ToolContext, url: str) -> str:
    """One page as readable text, or a bracketed failure string.

    Returned as a string rather than raised: a single dead link among a
    dozen candidates is routine and expected, and the research model
    needs to see the failure as data to route around -- the same
    "a failing probe is data, not a crash" principle the original applied,
    now scoped to the narrow exceptions HttpClient actually raises rather
    than a blind except Exception (CLAUDE.md rule 2.1).
    """
    try:
        response = await ctx.http.get(url, respect_robots=True)
    except RobotsDisallowedError:
        return f"[fetch failed: {url} -- disallowed by robots.txt]"
    except UpstreamError as exc:
        return f"[fetch failed: {url} -- {exc}]"

    if response.status_code != 200:
        return f"[fetch failed: {url} -- HTTP {response.status_code}]"
    return f"[fetched {url}]\n{_html_to_text(response.text)[:MAX_PAGE_CHARS]}"


def _render_hits(results: list[SearchResult]) -> str:
    if not results:
        return "  (no results)"
    return "\n".join(f"- {r.title}\n  {r.url}\n  {r.snippet}" for r in results)


# --------------------------------------------------------------- website ---


async def scan_website(ctx: ToolContext, website: str, max_pages: int = DEFAULT_MAX_PAGES) -> str:
    """Homepage plus the most promising news-bearing subpages, fetched in
    parallel.
    """
    if not website or not website.strip():
        return "[no website on file for this lead]"

    website = website.strip()
    home_text = await _fetch_text(ctx, website)
    parts = [f"### Homepage\n{home_text}"]

    base = _origin(website)
    # Even when the homepage fetch failed, the guessed paths are still
    # worth trying -- sometimes the root 404s but /blog resolves.
    candidates = _discover_links(home_text, base) if not home_text.startswith("[fetch") else []
    for path in NEWS_PATHS:
        url = base + path
        if url not in candidates:
            candidates.append(url)

    picked = candidates[:max_pages]
    if picked:
        fetched = await asyncio.gather(*(_fetch_text(ctx, url) for url in picked))
        for url, body in zip(picked, fetched, strict=True):
            if not body.startswith("[fetch failed"):
                parts.append(f"### {url}\n{body[:6000]}")

    return "\n\n".join(parts)[: MAX_PAGE_CHARS * 2]


# ----------------------------------------------------------------- socials ---


async def scan_social(ctx: ToolContext, clinic: str, city: str = "", handle_hint: str = "") -> str:
    """Instagram, Facebook and LinkedIn via the search index only -- see
    this module's docstring, point 1.
    """
    clinic = (clinic or "").strip()
    if not clinic:
        return "[no clinic name given]"

    loc = (city or "").strip()
    queries = [
        ("Instagram", f'site:instagram.com "{clinic}" {loc}'.strip()),
        ("Facebook", f'site:facebook.com "{clinic}" {loc}'.strip()),
        ("LinkedIn", f'site:linkedin.com "{clinic}"'.strip()),
        (
            "Recent social mentions",
            f'"{clinic}" {loc} (instagram OR facebook) new OR opening OR award OR welcome'.strip(),
        ),
    ]

    blocks: list[str] = []
    for label, query in queries:
        results = await ctx.search.search(query, limit=MAX_SEARCH_RESULTS)
        blocks.append(f"### {label}\nquery: {query}\n{_render_hits(results)}")

    blocks.append(
        "### How to read this\n"
        "Instagram, Facebook and LinkedIn all block unauthenticated scraping, so "
        "the snippets above are the index's copy of each profile -- usually the "
        "bio, counts, and sometimes recent caption text. A bio line is NOT news. "
        "Only treat this as a hook if a snippet states a specific dated event."
    )
    return "\n\n".join(blocks)


# ------------------------------------------------------- press and listings ---


async def scan_news(ctx: ToolContext, clinic: str, city: str = "", clinic_type: str = "") -> str:
    """Local press, award directories, and Google-Business-style
    announcements. The channel that most often finds something the
    company's own site never mentions.
    """
    clinic = (clinic or "").strip()
    loc = (city or "").strip()
    kind = (clinic_type or "").strip()

    queries = [
        f'"{clinic}" {loc} ("new location" OR "now open" OR expands OR opening)',
        f'"{clinic}" {loc} (award OR "best of" OR winner OR named OR honored)',
        f'"{clinic}" {loc} (welcomes OR "joins the team" OR "new provider" OR anniversary)',
        f'"{clinic}" {kind} {loc} news'.strip(),
    ]

    blocks = []
    for query in queries:
        results = await ctx.search.search(query, limit=MAX_SEARCH_RESULTS)
        blocks.append(f"query: {query}\n{_render_hits(results)}")
    return "### Press, awards and announcements\n" + "\n\n".join(blocks)


# ------------------------------------------------------------- free-form ---


async def search_web(ctx: ToolContext, query: str) -> str:
    results = await ctx.search.search(query, limit=MAX_SEARCH_RESULTS + 2)
    return _render_hits(results) if results else "(no results)"


async def fetch_page(ctx: ToolContext, url: str) -> str:
    return await _fetch_text(ctx, url)


def build_tools() -> list[Tool]:
    """Declarations only -- execution is dispatched by
    app.agents.hooks.agent against a bound ToolContext.
    """
    return [
        Tool(
            name="scan_website",
            description=(
                "Fetch a company's homepage plus its news-bearing subpages (blog, "
                "news, about, team, locations, specials) in one call. Use this "
                "first when the lead has a website."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "website": {"type": "string", "description": "The company's website URL."},
                },
                "required": ["website"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="scan_social",
            description=(
                "Look up the company on Instagram, Facebook and LinkedIn via the "
                "search index -- these platforms block direct scraping, so this "
                "returns what the index holds for each profile: bio, counts, and "
                "often recent caption text. Always run this; a website alone is "
                "not enough."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "clinic": {"type": "string", "description": "Company name."},
                    "city": {"type": "string", "description": "City or region. May be empty."},
                    "handle_hint": {
                        "type": "string",
                        "description": "Known Instagram handle, or empty.",
                    },
                },
                "required": ["clinic", "city", "handle_hint"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="scan_news",
            description=(
                "Search local press, award directories and announcement wording "
                "(new location, now open, welcomes, anniversary, best of) for this "
                "company. This channel most often finds items the company's own "
                "site never mentions."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "clinic": {"type": "string"},
                    "city": {"type": "string"},
                    "clinic_type": {
                        "type": "string",
                        "description": "e.g. dental clinic. May be empty.",
                    },
                },
                "required": ["clinic", "city", "clinic_type"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="search_web",
            description=(
                "Free-form web search for anything the batch tools did not cover "
                "-- verifying a date, chasing a specific claim, checking a "
                "directory listing."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query."},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="fetch_page",
            description=(
                "Fetch one specific URL as readable text. Use it to confirm a "
                "publication date or read an article a search snippet only teased."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Absolute URL to fetch."},
                },
                "required": ["url"],
                "additionalProperties": False,
            },
        ),
    ]


TOOL_FUNCTIONS: dict[str, Callable[..., Awaitable[str]]] = {
    "scan_website": scan_website,
    "scan_social": scan_social,
    "scan_news": scan_news,
    "search_web": search_web,
    "fetch_page": fetch_page,
}
