"""WebsiteEmailResolver and WebsitePersonResolver — the Resolver
implementations wrapping crawl_paths + extract_email + extract_person.

Both are Tier.FREE: crawling a company's own site costs nothing but time.
Both collect from every fetched page before returning — never stop at the
first hit, so a generic info@ on the homepage can't suppress an owner's
address found later on /team (the prototype's exact bug).

Escalates to Playwright only when looks_like_spa() says a plain fetch
returned an empty shell; launching a browser for every page would be
needlessly slow, and most pages don't need it.
"""

from __future__ import annotations

import contextlib
from decimal import Decimal

from app.core.errors import (
    DomainNotResolvableError,
    RobotsDisallowedError,
    UpstreamError,
)
from app.net.browser import BrowserFetcher, looks_like_spa
from app.net.client import HttpClient
from app.resolvers.base import BaseResolver, Candidate, LeadContext, Tier
from app.resolvers.company.crawl_paths import parse_sitemap, select_crawl_urls
from app.resolvers.company.extract_email import extract_emails
from app.resolvers.company.extract_person import extract_people


async def _sitemap_urls(ctx: LeadContext, http: HttpClient) -> list[str]:
    if not ctx.website:
        return []
    try:
        response = await http.get(f"{ctx.website.rstrip('/')}/sitemap.xml", respect_robots=False)
    except (UpstreamError, RobotsDisallowedError, DomainNotResolvableError):
        # No sitemap, one we can't fetch, or a domain that no longer
        # exists: degrade to blind path guessing rather than fail the
        # crawl over an optional hint.
        return []
    if response.status_code != 200:
        return []
    return parse_sitemap(response.text)


async def _fetch_pages(
    ctx: LeadContext, http: HttpClient, browser: BrowserFetcher | None
) -> list[tuple[str, str]]:
    """(url, html) for every crawl-path page reachable for this lead,
    escalating to a real browser only for pages that look like an empty
    SPA shell.

    A single missing page (404) or a robots-disallowed one is routine —
    most guessed paths won't exist for a given site — and must not stop
    the rest of the crawl. Caught narrowly (UpstreamError,
    RobotsDisallowedError), not with a blind `except Exception`
    (CLAUDE.md 2.1): a genuine systemic failure raises something else and
    propagates, showing up as an ERROR in the waterfall's telemetry for
    this resolver's whole resolve() call rather than vanishing silently.
    """
    if not ctx.website:
        return []

    sitemap_urls = await _sitemap_urls(ctx, http)
    urls = select_crawl_urls(ctx.website, sitemap_urls=sitemap_urls)

    pages: list[tuple[str, str]] = []
    for url in urls:
        try:
            response = await http.get(url, respect_robots=True)
        except (UpstreamError, RobotsDisallowedError):
            continue
        except DomainNotResolvableError:
            # The business's domain is gone. Every remaining path on this
            # host will fail identically, so stop rather than burning a
            # DNS timeout per guessed path. Returns no pages, which the
            # waterfall records as a MISS -- correct, because a lapsed
            # domain is a fact about the lead, not a failure of ours.
            return []
        if response.status_code != 200:
            continue

        page_html = response.text
        if browser is not None and looks_like_spa(page_html):
            # Playwright can raise any of several exception types (timeout,
            # navigation error, target-closed...); a browser escalation is
            # a bonus attempt on top of the fetch we already have, not
            # something the crawl depends on, so any failure here just
            # keeps the original static shell rather than losing the page.
            with contextlib.suppress(Exception):
                page_html = await browser.fetch(url)

        pages.append((url, page_html))

    return pages


class WebsiteEmailResolver(BaseResolver):
    name = "website_email"
    field = "email"
    tier = Tier.FREE
    cost_per_call = Decimal("0")
    jurisdictions: frozenset[str] | None = None  # worldwide

    def __init__(self, *, http: HttpClient, browser: BrowserFetcher | None = None) -> None:
        self._http = http
        self._browser = browser

    async def resolve(self, ctx: LeadContext) -> list[Candidate]:
        pages = await _fetch_pages(ctx, self._http, self._browser)

        candidates: list[Candidate] = []
        for url, page_html in pages:
            for hit in extract_emails(page_html, url):
                candidates.append(
                    Candidate(
                        value=hit.value,
                        confidence=hit.confidence,
                        source=self.name,
                        source_url=url,
                        extra={"strategy": hit.strategy, "is_role_account": hit.is_role_account},
                    )
                )
        return candidates


class WebsitePersonResolver(BaseResolver):
    name = "website_person"
    field = "person_name"
    tier = Tier.FREE
    cost_per_call = Decimal("0")
    jurisdictions: frozenset[str] | None = None  # worldwide

    def __init__(self, *, http: HttpClient, browser: BrowserFetcher | None = None) -> None:
        self._http = http
        self._browser = browser

    async def resolve(self, ctx: LeadContext) -> list[Candidate]:
        # Shares the same HttpClient (and therefore the same on-disk
        # response cache, Session 04) as WebsiteEmailResolver — whichever
        # of the two resolvers runs second sees cache hits, not a real
        # second crawl of the same site.
        pages = await _fetch_pages(ctx, self._http, self._browser)

        candidates: list[Candidate] = []
        for url, page_html in pages:
            for hit in extract_people(page_html, url):
                candidates.append(
                    Candidate(
                        value=hit.name,
                        confidence=hit.confidence,
                        source=self.name,
                        source_url=url,
                        extra={
                            "strategy": hit.strategy,
                            "title": hit.title,
                            "role_class": hit.role_class.value,
                        },
                    )
                )
        return candidates
