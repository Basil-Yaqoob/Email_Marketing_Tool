"""Unit tests for the HTTP layer: client, cache, rate limiter, robots and
the SPA heuristic. Network is always mocked with respx — a test that makes
a real outbound request is a broken test (doc/03-TESTING.md).

Test 7 (cache_hit_makes_no_network_call) is the one that matters most: it
is what makes re-parsing free.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx
import pytest
import respx

from app.core.errors import RobotsDisallowedError, UpstreamError
from app.net.browser import looks_like_spa
from app.net.cache import ResponseCache
from app.net.client import HttpClient
from app.net.ratelimit import RateLimiter
from app.net.robots import RobotsChecker

USER_AGENT = "EmailMarketingToolBot/1.0 (+https://example.invalid/bot)"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "html"


async def _no_sleep(delay: float) -> None:
    """A sleep stand-in that returns immediately. Retry/backoff tests need
    real backoff *logic* exercised, not real wall-clock waiting —
    doc/03-TESTING.md: no sleep() in tests, use fake clocks and injected time.
    """


def _make_client(
    tmp_path: Path,
    *,
    sleep: object = None,
    clock: object = None,
    requests_per_second: float = 1000.0,
    burst: int = 1000,
) -> HttpClient:
    cache_kwargs = {} if clock is None else {"clock": clock}
    cache = ResponseCache(tmp_path / "cache", **cache_kwargs)  # type: ignore[arg-type]
    limiter = RateLimiter(
        requests_per_second=requests_per_second, burst=burst, global_concurrency=1000
    )
    robots = RobotsChecker(user_agent=USER_AGENT)
    return HttpClient(
        cache=cache,
        limiter=limiter,
        robots=robots,
        user_agent=USER_AGENT,
        sleep=sleep,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------
# Client: happy path, retries, backoff
# --------------------------------------------------------------------------


@respx.mock
async def test_get_returns_body_on_200(tmp_path: Path) -> None:
    respx.get("https://example.com/page").mock(return_value=httpx.Response(200, text="hello world"))
    client = _make_client(tmp_path)
    try:
        response = await client.get("https://example.com/page", respect_robots=False)
    finally:
        await client.aclose()

    assert response.status_code == 200
    assert response.text == "hello world"


@respx.mock
async def test_retries_on_500_then_succeeds(tmp_path: Path) -> None:
    route = respx.get("https://example.com/flaky")
    route.side_effect = [
        httpx.Response(500),
        httpx.Response(500),
        httpx.Response(200, text="ok"),
    ]
    client = _make_client(tmp_path, sleep=_no_sleep)
    try:
        response = await client.get("https://example.com/flaky", respect_robots=False)
    finally:
        await client.aclose()

    assert response.status_code == 200
    assert route.call_count == 3


@respx.mock
async def test_does_not_retry_on_404(tmp_path: Path) -> None:
    route = respx.get("https://example.com/missing").mock(return_value=httpx.Response(404))
    client = _make_client(tmp_path, sleep=_no_sleep)
    try:
        with pytest.raises(UpstreamError):
            await client.get("https://example.com/missing", respect_robots=False)
    finally:
        await client.aclose()

    assert route.call_count == 1


@respx.mock
async def test_honours_retry_after_header_on_429(tmp_path: Path) -> None:
    route = respx.get("https://example.com/limited")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(200, text="ok"),
    ]
    delays: list[float] = []

    async def recording_sleep(delay: float) -> None:
        delays.append(delay)

    client = _make_client(tmp_path, sleep=recording_sleep)
    try:
        response = await client.get("https://example.com/limited", respect_robots=False)
    finally:
        await client.aclose()

    assert response.status_code == 200
    assert delays == [7.0]


@respx.mock
async def test_gives_up_after_max_attempts(tmp_path: Path) -> None:
    route = respx.get("https://example.com/dead").mock(return_value=httpx.Response(500))
    client = _make_client(tmp_path, sleep=_no_sleep)
    try:
        with pytest.raises(UpstreamError):
            await client.get("https://example.com/dead", respect_robots=False)
    finally:
        await client.aclose()

    assert route.call_count == 3


@respx.mock
async def test_backoff_includes_jitter(tmp_path: Path) -> None:
    respx.get("https://example.com/a").mock(return_value=httpx.Response(500))
    respx.get("https://example.com/b").mock(return_value=httpx.Response(500))

    delays_a: list[float] = []
    delays_b: list[float] = []

    async def record_a(delay: float) -> None:
        delays_a.append(delay)

    async def record_b(delay: float) -> None:
        delays_b.append(delay)

    client_a = _make_client(tmp_path, sleep=record_a)
    client_b = _make_client(tmp_path, sleep=record_b)
    try:
        with pytest.raises(UpstreamError):
            await client_a.get("https://example.com/a", respect_robots=False)
        with pytest.raises(UpstreamError):
            await client_b.get("https://example.com/b", respect_robots=False)
    finally:
        await client_a.aclose()
        await client_b.aclose()

    assert len(delays_a) == 2
    assert len(delays_b) == 2
    assert delays_a != delays_b, "two independent runs produced identical delays -- no jitter?"


@respx.mock
async def test_retries_on_timeout_then_succeeds(tmp_path: Path) -> None:
    route = respx.get("https://example.com/slow")
    route.side_effect = [httpx.TimeoutException("timed out"), httpx.Response(200, text="ok")]
    client = _make_client(tmp_path, sleep=_no_sleep)
    try:
        response = await client.get("https://example.com/slow", respect_robots=False)
    finally:
        await client.aclose()

    assert response.status_code == 200
    assert route.call_count == 2


@respx.mock
async def test_gives_up_after_repeated_timeouts(tmp_path: Path) -> None:
    route = respx.get("https://example.com/always-slow").mock(
        side_effect=httpx.TimeoutException("timed out")
    )
    client = _make_client(tmp_path, sleep=_no_sleep)
    try:
        with pytest.raises(UpstreamError):
            await client.get("https://example.com/always-slow", respect_robots=False)
    finally:
        await client.aclose()

    assert route.call_count == 3


@respx.mock
async def test_gives_up_after_max_429_attempts(tmp_path: Path) -> None:
    route = respx.get("https://example.com/always-limited").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "0"})
    )
    client = _make_client(tmp_path, sleep=_no_sleep)
    try:
        with pytest.raises(UpstreamError):
            await client.get("https://example.com/always-limited", respect_robots=False)
    finally:
        await client.aclose()

    assert route.call_count == 3


@respx.mock
async def test_retry_after_missing_falls_back_to_backoff(tmp_path: Path) -> None:
    route = respx.get("https://example.com/limited-no-header")
    route.side_effect = [httpx.Response(429), httpx.Response(200, text="ok")]
    delays: list[float] = []

    async def recording_sleep(delay: float) -> None:
        delays.append(delay)

    client = _make_client(tmp_path, sleep=recording_sleep)
    try:
        response = await client.get("https://example.com/limited-no-header", respect_robots=False)
    finally:
        await client.aclose()

    assert response.status_code == 200
    assert delays == [1.0]  # backoff base for attempt 1, no jitter on the retry-after path


@respx.mock
async def test_retry_after_invalid_falls_back_to_backoff(tmp_path: Path) -> None:
    route = respx.get("https://example.com/limited-bad-header")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "not-a-number"}),
        httpx.Response(200, text="ok"),
    ]
    delays: list[float] = []

    async def recording_sleep(delay: float) -> None:
        delays.append(delay)

    client = _make_client(tmp_path, sleep=recording_sleep)
    try:
        response = await client.get("https://example.com/limited-bad-header", respect_robots=False)
    finally:
        await client.aclose()

    assert response.status_code == 200
    assert delays == [1.0]


@respx.mock
async def test_client_usable_as_async_context_manager(tmp_path: Path) -> None:
    respx.get("https://example.com/ctx").mock(return_value=httpx.Response(200, text="ok"))
    async with _make_client(tmp_path) as client:
        response = await client.get("https://example.com/ctx", respect_robots=False)

    assert response.status_code == 200


@respx.mock
async def test_response_text_defaults_to_utf8_without_charset(tmp_path: Path) -> None:
    respx.get("https://example.com/no-charset").mock(
        return_value=httpx.Response(200, content=b"hello", headers={"content-type": "text/plain"})
    )
    client = _make_client(tmp_path)
    try:
        response = await client.get("https://example.com/no-charset", respect_robots=False)
    finally:
        await client.aclose()

    assert response.text == "hello"


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------


@respx.mock
async def test_cache_hit_makes_no_network_call(tmp_path: Path) -> None:
    route = respx.get("https://example.com/cached").mock(
        return_value=httpx.Response(200, text="cached body")
    )
    client = _make_client(tmp_path)
    try:
        first = await client.get("https://example.com/cached", respect_robots=False)
        second = await client.get("https://example.com/cached", respect_robots=False)
    finally:
        await client.aclose()

    assert first.body == second.body
    assert second.from_cache is True
    assert route.call_count == 1


@respx.mock
async def test_cache_respects_ttl(tmp_path: Path) -> None:
    route = respx.get("https://example.com/ttl")
    route.side_effect = [
        httpx.Response(200, text="first"),
        httpx.Response(200, text="second"),
    ]

    fake_now = [1_000_000.0]

    def clock() -> float:
        return fake_now[0]

    client = _make_client(tmp_path, clock=clock)
    try:
        first = await client.get("https://example.com/ttl", respect_robots=False, ttl_seconds=10)
        assert first.text == "first"
        assert first.from_cache is False

        fake_now[0] += 20  # advance the fake clock well past the 10s ttl

        second = await client.get("https://example.com/ttl", respect_robots=False, ttl_seconds=10)
        assert second.text == "second"
        assert second.from_cache is False
    finally:
        await client.aclose()

    assert route.call_count == 2


@respx.mock
async def test_cache_bypass_forces_fetch(tmp_path: Path) -> None:
    route = respx.get("https://example.com/bypass")
    route.side_effect = [
        httpx.Response(200, text="first"),
        httpx.Response(200, text="second"),
    ]
    client = _make_client(tmp_path)
    try:
        first = await client.get("https://example.com/bypass", respect_robots=False)
        bypassed = await client.get(
            "https://example.com/bypass", respect_robots=False, use_cache=False
        )
        # A later default (cached) call should now see the refreshed value
        # -- bypass forces a fresh fetch *and* updates the cache for later.
        third = await client.get("https://example.com/bypass", respect_robots=False)
    finally:
        await client.aclose()

    assert first.text == "first"
    assert bypassed.text == "second"
    assert third.text == "second"
    assert third.from_cache is True
    assert route.call_count == 2


@respx.mock
async def test_cache_stores_raw_bytes_not_decoded_text(tmp_path: Path) -> None:
    raw_body = "café".encode("latin-1")
    respx.get("https://example.com/latin1").mock(
        return_value=httpx.Response(
            200, content=raw_body, headers={"content-type": "text/plain; charset=latin-1"}
        )
    )
    client = _make_client(tmp_path)
    try:
        first = await client.get("https://example.com/latin1", respect_robots=False)
        second = await client.get("https://example.com/latin1", respect_robots=False)
    finally:
        await client.aclose()

    assert first.body == raw_body
    assert second.body == raw_body
    assert second.from_cache is True
    assert second.text == "café"


# --------------------------------------------------------------------------
# Rate limiter
# --------------------------------------------------------------------------


async def test_rate_limiter_holds_under_concurrency() -> None:
    limiter = RateLimiter(requests_per_second=10.0, burst=2, global_concurrency=50)

    async def hit() -> None:
        async with limiter.acquire("example.com"):
            pass

    start = time.monotonic()
    await asyncio.gather(*(hit() for _ in range(10)))
    elapsed = time.monotonic() - start

    # burst=2 lets the first 2 through immediately; the remaining 8 must
    # each wait ~1/rate = 0.1s apart for a fresh token.
    assert elapsed >= 0.7, f"10 requests at burst=2, rate=10/s took only {elapsed:.3f}s"


async def test_rate_limiter_is_per_host() -> None:
    limiter = RateLimiter(requests_per_second=2.0, burst=1, global_concurrency=50)

    async def hit(host: str) -> None:
        async with limiter.acquire(host):
            pass

    # Exhaust each host's single burst token first.
    await hit("a.example.com")
    await hit("b.example.com")

    start = time.monotonic()
    await asyncio.gather(hit("a.example.com"), hit("b.example.com"))
    elapsed = time.monotonic() - start

    # Each host needs ~1/rate = 0.5s for its next token. If the two hosts
    # shared one bucket (serialized), this would take ~1.0s instead.
    assert elapsed < 0.8, f"two hosts took {elapsed:.3f}s -- shared bucket instead of per-host?"


# --------------------------------------------------------------------------
# robots.txt
# --------------------------------------------------------------------------


@respx.mock
async def test_robots_disallow_raises(tmp_path: Path) -> None:
    respx.get("https://example.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private/")
    )
    client = _make_client(tmp_path)
    try:
        with pytest.raises(RobotsDisallowedError):
            await client.get("https://example.com/private/page", respect_robots=True)
    finally:
        await client.aclose()


@respx.mock
async def test_robots_override_allows_fetch(tmp_path: Path) -> None:
    respx.get("https://example.com/private/page").mock(
        return_value=httpx.Response(200, text="secret")
    )
    client = _make_client(tmp_path)
    try:
        # respect_robots=False must skip the robots check entirely -- no
        # request to /robots.txt is mocked, so respx would fail the test
        # if the client tried to fetch it anyway.
        response = await client.get("https://example.com/private/page", respect_robots=False)
    finally:
        await client.aclose()

    assert response.status_code == 200
    assert response.text == "secret"


@respx.mock
async def test_missing_robots_fails_open(tmp_path: Path) -> None:
    respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://example.com/page").mock(return_value=httpx.Response(200, text="ok"))
    client = _make_client(tmp_path)
    try:
        response = await client.get("https://example.com/page", respect_robots=True)
    finally:
        await client.aclose()

    assert response.status_code == 200


@respx.mock
async def test_robots_fetched_once_per_host_then_cached(tmp_path: Path) -> None:
    robots_route = respx.get("https://example.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nAllow: /")
    )
    respx.get("https://example.com/one").mock(return_value=httpx.Response(200, text="one"))
    respx.get("https://example.com/two").mock(return_value=httpx.Response(200, text="two"))

    client = _make_client(tmp_path)
    try:
        first = await client.get("https://example.com/one", respect_robots=True)
        second = await client.get("https://example.com/two", respect_robots=True)
    finally:
        await client.aclose()

    assert first.status_code == 200
    assert second.status_code == 200
    assert robots_route.call_count == 1, "robots.txt should be fetched once per host, then cached"


@respx.mock
async def test_robots_connection_error_fails_open(tmp_path: Path) -> None:
    respx.get("https://example.com/robots.txt").mock(side_effect=httpx.ConnectError("refused"))
    respx.get("https://example.com/page").mock(return_value=httpx.Response(200, text="ok"))

    client = _make_client(tmp_path)
    try:
        response = await client.get("https://example.com/page", respect_robots=True)
    finally:
        await client.aclose()

    assert response.status_code == 200


# --------------------------------------------------------------------------
# SPA detection
# --------------------------------------------------------------------------


def test_looks_like_spa_detects_empty_shell() -> None:
    spa_shell = (FIXTURES / "spa_no_content.html").read_text(encoding="utf-8")
    normal_page = (FIXTURES / "normal_content_page.html").read_text(encoding="utf-8")

    assert looks_like_spa(spa_shell) is True
    assert looks_like_spa(normal_page) is False
