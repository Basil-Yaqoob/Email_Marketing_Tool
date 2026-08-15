"""Unit tests for SERP person discovery, Impressum extraction, Instagram
business emails, and Google review-response names. Network is always
mocked — either with respx for the search-backend HTTP layer, or with a
plain in-memory SearchBackend double for resolver-level tests, since
SearchBackend is a Protocol and doesn't need real HTTP to exercise the
parsing/matching logic that actually matters here.

The LinkedIn/Instagram "never fetch that host" guards live in
tests/unit/test_no_linkedin_fetch.py, not here — see that file's
docstring for why it's kept separate.

Test 20 (test_serp_person_yield_floor) is the yield canary for this
session, same role as Session 06/07's yield floors.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import respx

from app.db.models.enums import RoleClass
from app.net.search import (
    BraveSearchBackend,
    FallbackSearchBackend,
    SearchResult,
    SearXNGBackend,
)
from app.resolvers.base import Candidate, LeadContext, Tier
from app.resolvers.company.instagram import InstagramEmailResolver
from app.resolvers.merge import merge_candidates
from app.resolvers.person.impressum import extract_impressum
from app.resolvers.person.review_response import extract_review_response
from app.resolvers.person.serp import (
    CONF_SERP,
    INDEPENDENCE_KEY,
    QUERY_TEMPLATES,
    SERPPersonResolver,
    _company_matches,
    parse_linkedin_snippet,
)

USER_AGENT = "EmailMarketingToolBot/1.0 (+https://example.invalid/bot)"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
HTML_FIXTURES = FIXTURES / "html" / "crawler"
JSON_FIXTURES = FIXTURES / "json"


def _load_html(*parts: str) -> str:
    return (HTML_FIXTURES / Path(*parts)).read_text(encoding="utf-8")


def _load_json(*parts: str) -> dict | list:
    return json.loads((JSON_FIXTURES / Path(*parts)).read_text(encoding="utf-8"))


def _ctx(**overrides: object) -> LeadContext:
    defaults: dict[str, object] = {
        "company_id": uuid4(),
        "company_name": "Northgate Dental",
        "domain": "northgatedental.co.uk",
        "website": "https://northgatedental.co.uk",
        "country_code": "GB",
    }
    defaults.update(overrides)
    return LeadContext(**defaults)  # type: ignore[arg-type]


@dataclass
class _FakeSearchBackend:
    """A SearchBackend double keyed by exact query string, recording every
    call made — used wherever a test cares about *what* was searched or
    *how many times*, not real HTTP.
    """

    by_query: dict[str, list[SearchResult]] = field(default_factory=dict)
    default: list[SearchResult] = field(default_factory=list)
    name: str = "fake"
    tier: Tier = Tier.FREE
    cost_per_call: Decimal = Decimal("0")
    calls: list[str] = field(default_factory=list)

    async def search(self, query: str, *, limit: int = 10) -> list[SearchResult]:
        self.calls.append(query)
        return self.by_query.get(query, self.default)


def _result(
    title: str, *, url: str = "https://www.linkedin.com/in/x", snippet: str = ""
) -> SearchResult:
    return SearchResult(title=title, url=url, snippet=snippet or title, position=0)


# --------------------------------------------------------------------------
# Snippet parsing
# --------------------------------------------------------------------------


def test_parses_dash_separated_linkedin_snippet() -> None:
    hit = parse_linkedin_snippet("Jane Smith - CEO - Northgate Dental | LinkedIn")
    assert hit is not None
    assert hit.name == "Jane Smith"
    assert hit.title == "CEO"
    assert hit.company == "Northgate Dental"


def test_parses_en_dash_variant() -> None:
    # Uses an actual en dash, not a hyphen -- that's the whole point here.
    hit = parse_linkedin_snippet("Jane Smith – Owner – Northgate Dental Care Ltd - LinkedIn")  # noqa: RUF001
    assert hit is not None
    assert hit.name == "Jane Smith"
    assert hit.title == "Owner"
    assert hit.company == "Northgate Dental Care Ltd"


def test_parses_pipe_separated_variant() -> None:
    hit = parse_linkedin_snippet(
        "Northgate Dental | Jane Smith | LinkedIn", company_hint="Northgate Dental"
    )
    assert hit is not None
    assert hit.name == "Jane Smith"
    assert hit.company == "Northgate Dental"


def test_pipe_variant_without_hint_is_ambiguous_and_rejected() -> None:
    """Without a company_hint, "Company | Name" and "Name | Company" are
    genuinely indistinguishable by shape alone -- must not guess.
    """
    assert parse_linkedin_snippet("Northgate Dental | Jane Smith | LinkedIn") is None


def test_snippet_without_linkedin_suffix_is_rejected() -> None:
    assert parse_linkedin_snippet("Northgate Dental - Home") is None
    assert parse_linkedin_snippet("Some Random Article About Dentistry") is None


@respx.mock
async def test_rejects_snippet_for_a_different_company() -> None:
    backend = _FakeSearchBackend(
        default=[_result("Jane Smith - CEO - Some Other Business Entirely | LinkedIn")]
    )
    resolver = SERPPersonResolver(search=backend)
    candidates = await resolver.resolve(_ctx(company_name="Northgate Dental"))
    assert candidates == []


def test_company_matches_reuses_session_07_scoring() -> None:
    assert _company_matches("Northgate Dental Care Ltd", "Northgate Dental") is True
    assert _company_matches("Riverside Medical Centre Limited", "Northgate Dental") is False


@respx.mock
async def test_extracts_title_and_role_class() -> None:
    backend = _FakeSearchBackend(
        default=[_result("Jane Smith - CEO - Northgate Dental | LinkedIn")]
    )
    resolver = SERPPersonResolver(search=backend)
    candidates = await resolver.resolve(_ctx())

    assert len(candidates) == 1
    assert candidates[0].extra["title"] == "CEO"
    assert candidates[0].extra["role_class"] == RoleClass.OWNER.value


def test_serp_candidates_share_independence_key() -> None:
    """Two queries against the same search index are not two independent
    pieces of evidence -- merging them must not compound confidence
    (0.7 + 0.7 independent would be ~0.91 via noisy-or; sharing
    independence_key must keep it at 0.7).
    """
    a = Candidate(
        value="Jane Smith",
        confidence=CONF_SERP,
        source="serp_person",
        independence_key=INDEPENDENCE_KEY,
    )
    b = Candidate(
        value="Jane Smith",
        confidence=CONF_SERP,
        source="serp_person",
        independence_key=INDEPENDENCE_KEY,
    )
    merged = merge_candidates([a, b])
    assert merged[0].confidence == pytest.approx(CONF_SERP)


async def test_query_templates_tried_in_order() -> None:
    """Stops once a template yields an accepted match -- later templates'
    search() is never even called.
    """
    first_query = QUERY_TEMPLATES[0].format(company="Northgate Dental", city="")
    backend = _FakeSearchBackend(
        by_query={first_query: [_result("Jane Smith - CEO - Northgate Dental | LinkedIn")]}
    )
    resolver = SERPPersonResolver(search=backend)

    candidates = await resolver.resolve(_ctx(company_name="Northgate Dental"))

    assert candidates
    assert backend.calls == [first_query], "later templates must not run once a match is accepted"


async def test_second_template_runs_when_first_finds_nothing() -> None:
    backend = _FakeSearchBackend()  # every query returns []
    resolver = SERPPersonResolver(search=backend)

    candidates = await resolver.resolve(_ctx(company_name="Northgate Dental", known_facts={}))

    assert candidates == []
    # Template 2 needs {city}; with no known city it's skipped, so only
    # templates 1, 3 and 4 (company-only) are actually tried -- and all
    # of them, since none produced an accepted match to stop early on.
    expected = [
        QUERY_TEMPLATES[0].format(company="Northgate Dental", city=""),
        QUERY_TEMPLATES[2].format(company="Northgate Dental", city=""),
        QUERY_TEMPLATES[3].format(company="Northgate Dental", city=""),
    ]
    assert backend.calls == expected


async def test_city_template_only_used_when_city_known() -> None:
    with_city_query = QUERY_TEMPLATES[1].format(company="Northgate Dental", city="Austin")
    backend = _FakeSearchBackend()
    resolver = SERPPersonResolver(search=backend)

    await resolver.resolve(_ctx(company_name="Northgate Dental", known_facts={"city": "Austin"}))

    assert with_city_query in backend.calls

    backend_no_city = _FakeSearchBackend()
    resolver_no_city = SERPPersonResolver(search=backend_no_city)
    await resolver_no_city.resolve(_ctx(company_name="Northgate Dental", known_facts={}))
    assert with_city_query not in backend_no_city.calls


# --------------------------------------------------------------------------
# Search backends
# --------------------------------------------------------------------------


def test_searxng_backend_needs_no_key() -> None:
    """Constructor accepts no api_key/token param at all."""
    import inspect

    params = inspect.signature(SearXNGBackend.__init__).parameters
    assert "api_key" not in params
    assert "token" not in params


@respx.mock
async def test_searxng_backend_parses_results(tmp_path: Path) -> None:
    from app.net.cache import ResponseCache
    from app.net.client import HttpClient
    from app.net.ratelimit import RateLimiter
    from app.net.robots import RobotsChecker

    respx.get(url__regex=r"https://searx\.example\.test/search.*").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Jane Smith - CEO - Northgate Dental | LinkedIn",
                        "url": "https://www.linkedin.com/in/janesmith",
                        "content": "Jane Smith is the CEO of Northgate Dental.",
                    }
                ]
            },
        )
    )
    http = HttpClient(
        cache=ResponseCache(tmp_path / "cache"),
        limiter=RateLimiter(requests_per_second=1000.0, burst=1000, global_concurrency=1000),
        robots=RobotsChecker(user_agent=USER_AGENT),
        user_agent=USER_AGENT,
    )
    try:
        backend = SearXNGBackend(http=http, base_url="https://searx.example.test")
        results = await backend.search("test query")
    finally:
        await http.aclose()

    assert len(results) == 1
    assert results[0].title == "Jane Smith - CEO - Northgate Dental | LinkedIn"


@respx.mock
async def test_falls_back_to_next_backend_on_rate_limit(tmp_path: Path) -> None:
    """429 on Brave -> falls through to SearXNG automatically."""
    from app.net.cache import ResponseCache
    from app.net.client import HttpClient
    from app.net.ratelimit import RateLimiter
    from app.net.robots import RobotsChecker

    async def _no_sleep(delay: float) -> None:
        return None

    respx.get(url__regex=r"https://api\.search\.brave\.com/.*").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "0"})
    )
    respx.get(url__regex=r"https://searx\.example\.test/search.*").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Jane Smith - CEO - Northgate Dental | LinkedIn",
                        "url": "https://www.linkedin.com/in/janesmith",
                        "content": "",
                    }
                ]
            },
        )
    )
    http = HttpClient(
        cache=ResponseCache(tmp_path / "cache"),
        limiter=RateLimiter(requests_per_second=1000.0, burst=1000, global_concurrency=1000),
        robots=RobotsChecker(user_agent=USER_AGENT),
        user_agent=USER_AGENT,
        sleep=_no_sleep,
    )
    try:
        brave = BraveSearchBackend(http=http, api_key="brave-key")
        searxng = SearXNGBackend(http=http, base_url="https://searx.example.test")
        fallback = FallbackSearchBackend([brave, searxng])

        results = await fallback.search("test query")
    finally:
        await http.aclose()

    assert len(results) == 1
    assert results[0].title == "Jane Smith - CEO - Northgate Dental | LinkedIn"


@respx.mock
async def test_serper_backend_parses_results(tmp_path: Path) -> None:
    from app.net.cache import ResponseCache
    from app.net.client import HttpClient
    from app.net.ratelimit import RateLimiter
    from app.net.robots import RobotsChecker
    from app.net.search import SerperBackend

    respx.post("https://google.serper.dev/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "organic": [
                    {
                        "title": "Jane Smith - CEO - Northgate Dental | LinkedIn",
                        "link": "https://www.linkedin.com/in/janesmith",
                        "snippet": "Jane Smith is the CEO of Northgate Dental.",
                    }
                ]
            },
        )
    )
    http = HttpClient(
        cache=ResponseCache(tmp_path / "cache"),
        limiter=RateLimiter(requests_per_second=1000.0, burst=1000, global_concurrency=1000),
        robots=RobotsChecker(user_agent=USER_AGENT),
        user_agent=USER_AGENT,
    )
    try:
        backend = SerperBackend(http=http, api_key="serper-key")
        results = await backend.search("test query")
    finally:
        await http.aclose()

    assert len(results) == 1
    assert results[0].url == "https://www.linkedin.com/in/janesmith"
    assert backend.tier == Tier.METERED


def test_fallback_search_backend_requires_at_least_one() -> None:
    with pytest.raises(ValueError, match="at least one"):
        FallbackSearchBackend([])


def test_build_search_backend_defaults_to_searxng(tmp_path: Path) -> None:
    from app.core.config import Settings
    from app.net.cache import ResponseCache
    from app.net.client import HttpClient
    from app.net.ratelimit import RateLimiter
    from app.net.robots import RobotsChecker
    from app.net.search import build_search_backend

    settings = Settings(
        database_url="postgresql+asyncpg://u:p@localhost/db",
        redis_url="redis://localhost:6379/0",
        secret_key="test-secret",
        searxng_url="https://searx.example.test",
    )
    http = HttpClient(
        cache=ResponseCache(tmp_path / "cache"),
        limiter=RateLimiter(requests_per_second=1000.0, burst=1000, global_concurrency=1000),
        robots=RobotsChecker(user_agent=USER_AGENT),
        user_agent=USER_AGENT,
    )
    backend = build_search_backend(http=http, settings=settings)
    assert isinstance(backend, SearXNGBackend)


def test_build_search_backend_layers_keyed_backends_over_searxng(tmp_path: Path) -> None:
    from app.core.config import Settings
    from app.net.cache import ResponseCache
    from app.net.client import HttpClient
    from app.net.ratelimit import RateLimiter
    from app.net.robots import RobotsChecker
    from app.net.search import build_search_backend

    settings = Settings(
        database_url="postgresql+asyncpg://u:p@localhost/db",
        redis_url="redis://localhost:6379/0",
        secret_key="test-secret",
        searxng_url="https://searx.example.test",
        brave_search_api_key="brave-key",
    )
    http = HttpClient(
        cache=ResponseCache(tmp_path / "cache"),
        limiter=RateLimiter(requests_per_second=1000.0, burst=1000, global_concurrency=1000),
        robots=RobotsChecker(user_agent=USER_AGENT),
        user_agent=USER_AGENT,
    )
    backend = build_search_backend(http=http, settings=settings)
    assert isinstance(backend, FallbackSearchBackend)
    assert backend.name == "brave+searxng"


def test_build_search_backend_raises_without_any_configured() -> None:
    from app.core.config import Settings
    from app.core.errors import MissingConfigError
    from app.net.search import build_search_backend

    settings = Settings(
        database_url="postgresql+asyncpg://u:p@localhost/db",
        redis_url="redis://localhost:6379/0",
        secret_key="test-secret",
    )
    with pytest.raises(MissingConfigError):
        build_search_backend(http=None, settings=settings)  # type: ignore[arg-type]


@respx.mock
async def test_searxng_backend_raises_helpful_error_on_non_json(tmp_path: Path) -> None:
    from app.core.errors import UpstreamError
    from app.net.cache import ResponseCache
    from app.net.client import HttpClient
    from app.net.ratelimit import RateLimiter
    from app.net.robots import RobotsChecker

    respx.get(url__regex=r"https://searx\.example\.test/search.*").mock(
        return_value=httpx.Response(200, text="<html>format=json is not enabled</html>")
    )
    http = HttpClient(
        cache=ResponseCache(tmp_path / "cache"),
        limiter=RateLimiter(requests_per_second=1000.0, burst=1000, global_concurrency=1000),
        robots=RobotsChecker(user_agent=USER_AGENT),
        user_agent=USER_AGENT,
    )
    try:
        backend = SearXNGBackend(http=http, base_url="https://searx.example.test")
        with pytest.raises(UpstreamError, match="format"):
            await backend.search("test query")
    finally:
        await http.aclose()


# --------------------------------------------------------------------------
# Impressum
# --------------------------------------------------------------------------


def test_impressum_geschaeftsfuehrer() -> None:
    html = _load_html("impressum_de_mittelstands_agentur.html")
    hits = extract_impressum(html, "https://mittelstands-agentur.de/impressum/")
    names = {h.name for h in hits}
    assert "Stefan Müller" in names
    hit = next(h for h in hits if h.name == "Stefan Müller")
    assert hit.title == "Geschäftsführer"
    assert hit.role_class == RoleClass.OWNER


def test_impressum_vertreten_durch() -> None:
    html = _load_html("synthetic", "impressum_variants.html")
    hits = extract_impressum(html, "https://example.test/impressum")
    hit = next(h for h in hits if h.title == "Vertreten durch")
    assert hit.name == "Anna Fischer"


def test_impressum_inhaber() -> None:
    html = _load_html("synthetic", "impressum_variants.html")
    hits = extract_impressum(html, "https://example.test/impressum")
    hit = next(h for h in hits if h.title == "Inhaber")
    assert hit.name == "Thomas Weber"


def test_impressum_extracts_handelsregister_number() -> None:
    html = _load_html("impressum_de_mittelstands_agentur.html")
    hits = extract_impressum(html, "https://mittelstands-agentur.de/impressum/")
    numbers = {h.handelsregister_number for h in hits}
    assert any(n is not None and "34293" in n for n in numbers) or any(
        n is not None and "83756" in n for n in numbers
    )


def test_impressum_confidence_is_high() -> None:
    html = _load_html("impressum_de_mittelstands_agentur.html")
    hits = extract_impressum(html, "https://mittelstands-agentur.de/impressum/")
    assert hits
    assert all(h.confidence >= 0.90 for h in hits)


def test_impressum_extracts_registered_address() -> None:
    html = _load_html("impressum_de_mittelstands_agentur.html")
    hits = extract_impressum(html, "https://mittelstands-agentur.de/impressum/")
    addresses = {h.registered_address for h in hits if h.registered_address}
    assert any("50858" in a for a in addresses)


def test_impressum_pattern_rejects_non_name_shaped_text() -> None:
    html = """<html><body><p>Geschäftsführer: Support Team</p></body></html>"""
    hits = extract_impressum(html, "https://example.test/impressum")
    assert hits == []


def test_extract_address_returns_none_without_a_matching_pair() -> None:
    from app.resolvers.person.impressum import _extract_address

    assert _extract_address([]) is None
    assert _extract_address(["Not a street line", "Also not postal"]) is None
    # A street-shaped line with nothing usable on the following line.
    assert _extract_address(["Musterstraße 12", "Not a postal code line"]) is None


@respx.mock
async def test_impressum_resolver_end_to_end(tmp_path: Path) -> None:
    """resolve() actually crawls the site and extracts from real pages --
    not just the pure extract_impressum() function tested elsewhere.
    """
    from app.net.cache import ResponseCache
    from app.net.client import HttpClient
    from app.net.ratelimit import RateLimiter
    from app.net.robots import RobotsChecker
    from app.resolvers.company.crawl_paths import select_crawl_urls
    from app.resolvers.person.impressum import ImpressumResolver

    base = "https://beispiel.example.test"
    impressum_html = _load_html("impressum_de_mittelstands_agentur.html")
    impressum_url = f"{base}/impressum"

    # A real DE site publishing a sitemap is exactly how a guessed path
    # deep in the crawl-path list (default max_fetches=12) would still
    # get prioritised into the budget -- select_crawl_urls puts
    # sitemap-confirmed URLs first.
    sitemap_url = f"{base}/sitemap.xml"
    sitemap_xml = f"<urlset><url><loc>{impressum_url}</loc></url></urlset>"
    respx.get(sitemap_url).mock(return_value=httpx.Response(200, text=sitemap_xml))
    respx.get(f"{base}/robots.txt").mock(return_value=httpx.Response(404))
    for url in select_crawl_urls(base, sitemap_urls=[impressum_url]):
        route_url = url if url != base else f"{base}/"
        if route_url.endswith("/impressum") or route_url.endswith("/impressum/"):
            respx.get(route_url).mock(return_value=httpx.Response(200, text=impressum_html))
        else:
            respx.get(route_url).mock(return_value=httpx.Response(404))

    http = HttpClient(
        cache=ResponseCache(tmp_path / "cache"),
        limiter=RateLimiter(requests_per_second=1000.0, burst=1000, global_concurrency=1000),
        robots=RobotsChecker(user_agent=USER_AGENT),
        user_agent=USER_AGENT,
    )
    try:
        resolver = ImpressumResolver(http=http)
        candidates = await resolver.resolve(_ctx(website=base, country_code="DE"))
    finally:
        await http.aclose()

    assert candidates
    names = {c.value for c in candidates}
    assert "Stefan Müller" in names
    hit = next(c for c in candidates if c.value == "Stefan Müller")
    assert hit.confidence == pytest.approx(0.90)
    assert hit.extra["handelsregister_number"] is not None


# --------------------------------------------------------------------------
# Instagram
# --------------------------------------------------------------------------


async def test_instagram_email_from_search_index() -> None:
    backend = _FakeSearchBackend(
        default=[
            _result(
                "Northgate Dental (@northgatedental) • Instagram",
                url="https://www.instagram.com/northgatedental/",
                snippet="Book online or email hello@northgatedental.com for enquiries.",
            ),
            _result(
                "Some unrelated Instagram result",
                url="https://www.instagram.com/unrelated/",
                snippet="No email in this one at all.",
            ),
        ]
    )
    resolver = InstagramEmailResolver(search=backend)
    candidates = await resolver.resolve(_ctx())

    assert len(candidates) == 1
    assert candidates[0].value == "hello@northgatedental.com"
    assert candidates[0].confidence == pytest.approx(0.65)
    assert candidates[0].independence_key == "search_index"


async def test_instagram_resolver_filters_bad_hint_addresses() -> None:
    backend = _FakeSearchBackend(
        default=[
            _result(
                "Business Instagram",
                url="https://www.instagram.com/business/",
                snippet="Contact: noreply@sentry.io for platform issues.",
            )
        ]
    )
    resolver = InstagramEmailResolver(search=backend)
    candidates = await resolver.resolve(_ctx())
    assert candidates == []


# --------------------------------------------------------------------------
# Review response
# --------------------------------------------------------------------------


def test_review_response_extracts_signed_name() -> None:
    hit = extract_review_response("Thanks for visiting! - Dr. Sarah, Owner")
    assert hit is not None
    assert hit.name == "Dr. Sarah"
    assert hit.title == "Owner"
    assert hit.role_class == RoleClass.OWNER


@pytest.mark.parametrize(
    "text",
    [
        "Thank you for your feedback! - The Team",
        "We're sorry to hear that - Management",
        "Thanks so much - Front Desk",
        "Great, thanks for stopping by!",  # no signoff at all
        "- OK",  # too short / not name-shaped
    ],
)
def test_review_response_ignores_unsigned_replies(text: str) -> None:
    assert extract_review_response(text) is None


async def test_review_response_resolver_reads_known_facts() -> None:
    from app.resolvers.person.review_response import ReviewResponseResolver

    reviews = json.dumps(
        [
            "Thanks for the kind words! - Dr. Sarah, Owner",
            "We appreciate it - The Team",
            "So glad you enjoyed your visit - John Smith, Practice Manager",
        ]
    )
    resolver = ReviewResponseResolver()
    candidates = await resolver.resolve(_ctx(known_facts={"review_responses_json": reviews}))

    names = {c.value for c in candidates}
    assert names == {"Dr. Sarah", "John Smith"}
    assert all(c.confidence == pytest.approx(0.60) for c in candidates)


async def test_review_response_resolver_degrades_without_reviews() -> None:
    from app.resolvers.person.review_response import ReviewResponseResolver

    resolver = ReviewResponseResolver()
    assert await resolver.resolve(_ctx(known_facts={})) == []


async def test_review_response_resolver_degrades_on_malformed_json() -> None:
    from app.resolvers.person.review_response import ReviewResponseResolver

    resolver = ReviewResponseResolver()
    candidates = await resolver.resolve(
        _ctx(known_facts={"review_responses_json": "not valid json"})
    )
    assert candidates == []


# --------------------------------------------------------------------------
# Yield floor
# --------------------------------------------------------------------------


async def test_serp_person_yield_floor() -> None:
    """>=40% of 20 fixture companies yield a name — includes real miss
    cases (wrong-company match, non-LinkedIn snippet, empty results)
    rather than an inflated 100%.
    """
    corpus = _load_json("serp", "yield_corpus.json")

    hits = 0
    for entry in corpus:
        results = [
            SearchResult(
                title=r["title"], url=r["url"], snippet=r["snippet"], position=r["position"]
            )
            for r in entry["results"]
        ]
        backend = _FakeSearchBackend(default=results)
        resolver = SERPPersonResolver(search=backend)
        candidates = await resolver.resolve(_ctx(company_name=entry["company_name"]))
        if candidates:
            hits += 1

    yield_rate = hits / len(corpus)
    assert yield_rate >= 0.40, f"SERP person yield {yield_rate:.1%} below the 40% floor"
