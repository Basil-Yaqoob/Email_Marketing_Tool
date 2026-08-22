"""The async HTTP client every resolver from Session 05 onward uses.

Behaviour, in order, for every GET:
  1. robots.txt check (unless overridden) -> RobotsDisallowedError if blocked
  2. cache lookup -> return on hit, no network call at all
  3. rate limiter -> wait for a per-host token and a global concurrency slot
  4. request with timeout
  5. retry on 429/5xx/timeout with jittered exponential backoff, max 3 attempts
  6. 429 honours the Retry-After header instead of the backoff curve
  7. cache the response
  8. any other 4xx does not retry -- raises immediately

post() shares steps 3-6 with get() (rate limiting, retries, backoff) but
skips robots.txt (it's an API call, not a page fetch) and the cache (a
metered search result silently replayed from disk would defeat a budget
guard's purpose) — see Session 05's Google Places resolver.

Identify yourself honestly in the User-Agent. A contactable UA gets you
unblocked when someone notices unusual traffic; a fake Chrome string gets
you banned.
"""

from __future__ import annotations

import asyncio
import random
import socket
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self
from urllib.parse import urlsplit

import httpx

from app.core.errors import (
    DomainNotResolvableError,
    RobotsDisallowedError,
    UpstreamError,
)
from app.core.logging import get_logger
from app.net.cache import ResponseCache
from app.net.ratelimit import RateLimiter
from app.net.robots import RobotsChecker

log = get_logger(__name__)

MAX_ATTEMPTS = 3
RETRYABLE_5XX = {500, 502, 503, 504}
DEFAULT_TIMEOUT_SECONDS = 15.0

SleepFn = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Response:
    status_code: int
    headers: dict[str, str]
    body: bytes
    url: str
    from_cache: bool = False

    @property
    def text(self) -> str:
        return self.body.decode(self._encoding(), errors="replace")

    def _encoding(self) -> str:
        content_type = self.headers.get("content-type", "")
        if "charset=" in content_type:
            return content_type.split("charset=")[-1].split(";")[0].strip()
        return "utf-8"


def _is_dns_failure(exc: httpx.ConnectError) -> bool:
    """Whether a connect error was actually a name-resolution failure.

    httpx.ConnectError covers both "host does not exist" and "host exists
    but refused the connection", which mean different things. The socket
    layer distinguishes them with gaierror, so check the cause chain
    rather than pattern-matching the message, which varies by platform
    ("getaddrinfo failed" on Windows, "Name or service not known" on
    Linux).
    """
    cause: BaseException | None = exc
    while cause is not None:
        if isinstance(cause, socket.gaierror):
            return True
        cause = cause.__cause__
    return "getaddrinfo" in str(exc).lower() or "name or service not known" in str(exc).lower()


class HttpClient:
    def __init__(
        self,
        *,
        cache: ResponseCache,
        limiter: RateLimiter,
        robots: RobotsChecker,
        user_agent: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        sleep: SleepFn | None = None,
    ) -> None:
        self._cache = cache
        self._limiter = limiter
        self._robots = robots
        self._user_agent = user_agent
        self._timeout = timeout
        # Injectable so tests exercise the real retry/backoff/jitter logic
        # without actually waiting — see doc/03-TESTING.md: no sleep() in
        # tests, use fake clocks and injected time.
        self._sleep: SleepFn = sleep or asyncio.sleep
        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def get(
        self,
        url: str,
        *,
        use_cache: bool = True,
        respect_robots: bool = True,
        ttl_seconds: float | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> Response:
        """extra_headers exists for Session 07's registry APIs (Companies
        House's Basic Auth, EDGAR's required contact User-Agent) — plain
        page fetches never need it, so it defaults to None and every
        existing caller is unaffected.
        """
        if respect_robots:
            allowed = await self._robots.allowed(url)
            if not allowed:
                raise RobotsDisallowedError(url)

        if use_cache:
            cached = await self._cache.get("GET", url, ttl_seconds=ttl_seconds)
            if cached is not None:
                return Response(
                    status_code=cached.status_code,
                    headers=cached.headers,
                    body=cached.body,
                    url=url,
                    from_cache=True,
                )

        host = urlsplit(url).netloc
        async with self._limiter.acquire(host):
            response = await self._fetch_with_retries(url, extra_headers=extra_headers)

        # Always write back, even when use_cache=False: a caller bypassing
        # the cache to force a fresh fetch still wants later calls to see
        # the refreshed value, not the stale one bypass skipped past.
        await self._cache.put(
            "GET",
            url,
            status_code=response.status_code,
            headers=response.headers,
            body=response.body,
        )
        return response

    async def post(
        self,
        url: str,
        *,
        json: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        """POST with the same retry/backoff and per-host rate limiting as
        get(), for APIs that require a JSON request body (e.g. Google
        Places' searchText) rather than query parameters.

        Never cached — a metered search result being silently replayed
        from disk would defeat the budget guard's whole purpose (the
        caller tracks and pays for every call it actually makes) — and
        never subject to robots.txt, since this is an API call, not a
        page fetch.
        """
        host = urlsplit(url).netloc
        async with self._limiter.acquire(host):
            return await self._fetch_with_retries(
                url, method="POST", json_body=json, extra_headers=headers
            )

    async def _fetch_with_retries(
        self,
        url: str,
        *,
        method: str = "GET",
        json_body: Mapping[str, Any] | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> Response:
        headers: dict[str, str] = {"User-Agent": self._user_agent}
        if extra_headers:
            headers.update(extra_headers)

        for attempt in range(1, MAX_ATTEMPTS + 1):
            is_last = attempt == MAX_ATTEMPTS
            try:
                raw = await self._client.request(method, url, headers=headers, json=json_body)
            except httpx.TimeoutException as exc:
                if is_last:
                    raise UpstreamError(
                        f"timed out fetching {url} after {MAX_ATTEMPTS} attempts"
                    ) from exc
                await self._sleep_backoff(attempt)
                continue
            except httpx.ConnectError as exc:
                # A host that doesn't resolve is a dead domain, not an
                # upstream failure, and retrying it twice more just wastes
                # the DNS timeout. Raised immediately as its own type so
                # the batch guard treats it as a miss -- see
                # DomainNotResolvableError on why that distinction matters.
                if _is_dns_failure(exc):
                    raise DomainNotResolvableError(f"host does not resolve for {url}") from exc
                if is_last:
                    raise UpstreamError(
                        f"could not connect to {url} after {MAX_ATTEMPTS} attempts"
                    ) from exc
                await self._sleep_backoff(attempt)
                continue

            if raw.status_code == 429:
                if is_last:
                    raise UpstreamError(
                        f"rate limited fetching {url} after {MAX_ATTEMPTS} attempts"
                    )
                await self._sleep_retry_after(raw, attempt)
                continue

            if raw.status_code in RETRYABLE_5XX:
                if is_last:
                    raise UpstreamError(
                        f"{raw.status_code} fetching {url} after {MAX_ATTEMPTS} attempts"
                    )
                await self._sleep_backoff(attempt)
                continue

            if raw.status_code >= 400:
                # Any other 4xx: our fault or a deliberate block, not a
                # transient failure. Retrying a 403 just gets the IP
                # blocked faster.
                raise UpstreamError(f"{raw.status_code} fetching {url}")

            return Response(
                status_code=raw.status_code,
                headers=dict(raw.headers),
                body=raw.content,
                url=str(raw.url),
            )

        # Unreachable: the loop always returns or raises on its last
        # iteration. Satisfies mypy's "missing return" check.
        raise UpstreamError(f"gave up fetching {url}")  # pragma: no cover

    async def _sleep_backoff(self, attempt: int) -> None:
        base = 2 ** (attempt - 1)  # 1, 2, 4, ...
        jitter = random.uniform(0, base * 0.5)
        await self._sleep(base + jitter)

    async def _sleep_retry_after(self, response: httpx.Response, attempt: int) -> None:
        retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = 2 ** (attempt - 1)
        else:
            delay = 2 ** (attempt - 1)
        await self._sleep(delay)
