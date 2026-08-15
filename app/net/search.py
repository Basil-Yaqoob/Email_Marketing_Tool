"""Pluggable web search — the substrate SERPPersonResolver and the
Instagram/social resolvers are built on.

Every backend returns the same SearchResult shape (title, url, snippet,
position). Callers never see raw provider JSON, and — critically — never
see a fetched page from the site being searched. The LinkedIn/Instagram
profile itself is never requested; only the search index's own snippet is
read. See CLAUDE.md §10 and doc/00-OVERVIEW.md for why that distinction is
the whole point of this module.

SearXNG (self-hosted, free, no key) is the zero-cost default. Brave and
Serper are optional paid/keyed escalations, tried first when configured —
FallbackSearchBackend falls through to the next backend on a rate limit or
upstream failure, so a metered backend running dry doesn't stall a batch.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable
from urllib.parse import quote

from app.core.config import Settings
from app.core.errors import MissingConfigError, UpstreamError
from app.core.logging import get_logger
from app.net.client import HttpClient
from app.resolvers.base import Tier

log = get_logger(__name__)

SEARXNG_FORMAT_HINT = (
    "SearXNG returned non-JSON — check that `search: formats: [json]` (or "
    "the older `format=json`) is enabled in its settings.yml; it's off by "
    "default and a misconfigured instance 403s or serves HTML instead."
)

# Serper's published per-search price at time of writing — update if their
# pricing changes, same pattern as PlacesBudget's COST_PER_REQUEST.
SERPER_COST_PER_REQUEST = Decimal("0.001")


@dataclass(frozen=True, slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    position: int


@runtime_checkable
class SearchBackend(Protocol):
    # Declared as read-only properties, not plain attributes: a class
    # attribute (SearXNGBackend.name = "searxng") satisfies a read-only
    # property requirement structurally, but FallbackSearchBackend
    # computes these from its wrapped backends via @property, which only
    # satisfies a settable-attribute requirement if the Protocol itself
    # says "read-only" too.
    @property
    def name(self) -> str: ...
    @property
    def tier(self) -> Tier: ...
    @property
    def cost_per_call(self) -> Decimal: ...

    async def search(self, query: str, *, limit: int = 10) -> list[SearchResult]: ...


class SearXNGBackend:
    """Self-hosted, no key, no quota — the default every install can use
    with nothing configured beyond a running instance.
    """

    name = "searxng"
    tier = Tier.FREE
    cost_per_call = Decimal("0")

    def __init__(self, *, http: HttpClient, base_url: str) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")

    async def search(self, query: str, *, limit: int = 10) -> list[SearchResult]:
        response = await self._http.get(
            f"{self._base_url}/search?q={quote(query)}&format=json",
            respect_robots=False,  # our own API call to our own instance
            use_cache=False,  # search results go stale fast; never serve day-old rankings
        )
        try:
            data = json.loads(response.text)
        except json.JSONDecodeError as exc:
            raise UpstreamError(SEARXNG_FORMAT_HINT) from exc

        results = []
        for i, item in enumerate(data.get("results", [])[:limit]):
            results.append(
                SearchResult(
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    snippet=item.get("content", ""),
                    position=i,
                )
            )
        return results


class BraveSearchBackend:
    """Brave Search API — generous free tier, but still needs the user's
    own key, so it's OWNED tier rather than FREE.
    """

    name = "brave"
    tier = Tier.OWNED
    cost_per_call = Decimal("0")

    def __init__(self, *, http: HttpClient, api_key: str) -> None:
        self._http = http
        self._api_key = api_key

    async def search(self, query: str, *, limit: int = 10) -> list[SearchResult]:
        response = await self._http.get(
            f"https://api.search.brave.com/res/v1/web/search?q={quote(query)}&count={limit}",
            extra_headers={
                "X-Subscription-Token": self._api_key,
                "Accept": "application/json",
            },
            respect_robots=False,
            use_cache=False,
        )
        data = json.loads(response.text)
        results = []
        for i, item in enumerate(data.get("web", {}).get("results", [])[:limit]):
            results.append(
                SearchResult(
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    snippet=item.get("description", ""),
                    position=i,
                )
            )
        return results


class SerperBackend:
    """Serper — cheap, reliable, metered. Uses HttpClient.post() since
    Serper's API is JSON-body, not query-string (same shape as Session
    05's Google Places resolver).
    """

    name = "serper"
    tier = Tier.METERED
    cost_per_call = SERPER_COST_PER_REQUEST

    def __init__(self, *, http: HttpClient, api_key: str) -> None:
        self._http = http
        self._api_key = api_key

    async def search(self, query: str, *, limit: int = 10) -> list[SearchResult]:
        response = await self._http.post(
            "https://google.serper.dev/search",
            json={"q": query, "num": limit},
            headers={"X-API-KEY": self._api_key, "Content-Type": "application/json"},
        )
        data = json.loads(response.text)
        results = []
        for i, item in enumerate(data.get("organic", [])[:limit]):
            results.append(
                SearchResult(
                    title=item.get("title", ""),
                    url=item.get("link", ""),
                    snippet=item.get("snippet", ""),
                    position=i,
                )
            )
        return results


class FallbackSearchBackend:
    """Tries each backend in order, falling through to the next on an
    upstream failure (rate limit, 5xx, timeout — anything HttpClient's own
    retry/backoff already gave up on). A metered backend running dry costs
    one wasted call and degrades to the next source, not a broken batch.
    """

    def __init__(self, backends: Sequence[SearchBackend]) -> None:
        if not backends:
            raise ValueError("FallbackSearchBackend needs at least one backend")
        self._backends = list(backends)

    @property
    def name(self) -> str:
        return "+".join(b.name for b in self._backends)

    @property
    def tier(self) -> Tier:
        return self._backends[0].tier

    @property
    def cost_per_call(self) -> Decimal:
        return self._backends[0].cost_per_call

    async def search(self, query: str, *, limit: int = 10) -> list[SearchResult]:
        last_error: Exception | None = None
        for backend in self._backends:
            try:
                return await backend.search(query, limit=limit)
            except UpstreamError as exc:
                last_error = exc
                log.warning("search.backend_failed", backend=backend.name, error=str(exc))
                continue
        raise UpstreamError(f"all search backends failed: {last_error}")


def build_search_backend(*, http: HttpClient, settings: Settings) -> SearchBackend:
    """Keyed backends are layered on top of the zero-cost SearXNG default,
    tried first (better quality, higher limits) and falling back to
    SearXNG on failure — the prototype's exact ergonomics: setting a paid
    provider's key switches search to it automatically, no code change.
    """
    chain: list[SearchBackend] = []
    if settings.serper_api_key is not None:
        chain.append(SerperBackend(http=http, api_key=settings.serper_api_key.get_secret_value()))
    if settings.brave_search_api_key is not None:
        chain.append(
            BraveSearchBackend(http=http, api_key=settings.brave_search_api_key.get_secret_value())
        )
    if settings.searxng_url:
        chain.append(SearXNGBackend(http=http, base_url=settings.searxng_url))

    if not chain:
        raise MissingConfigError("searxng_url")
    if len(chain) == 1:
        return chain[0]
    return FallbackSearchBackend(chain)
