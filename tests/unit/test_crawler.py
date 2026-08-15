"""Unit tests for the crawler: email/person extraction, crawl path
selection, and the WebsiteEmailResolver/WebsitePersonResolver Resolver
implementations. Network is always mocked with respx.

Fixtures under tests/fixtures/html/crawler/ are real pages, fetched live
and saved verbatim — see the provenance comment at the top of each file.
Four are synthetic (tests/fixtures/html/crawler/synthetic/), and say so —
see that directory's README for why a live example wasn't used.

Tests 20 and 21 are the canaries: the numbers Phase 1 exists to move. If a
refactor drops them, the build has regressed even though nothing threw.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from app.net.cache import ResponseCache
from app.net.client import HttpClient
from app.net.ratelimit import RateLimiter
from app.net.robots import RobotsChecker
from app.resolvers.company.crawl_paths import parse_sitemap, select_crawl_urls
from app.resolvers.company.extract_email import ROLE_PREFIXES, extract_emails
from app.resolvers.company.extract_person import extract_people
from app.resolvers.company.website import WebsiteEmailResolver, WebsitePersonResolver

USER_AGENT = "EmailMarketingToolBot/1.0 (+https://example.invalid/bot)"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "html" / "crawler"
SYNTHETIC = FIXTURES / "synthetic"
YIELD_CORPUS = FIXTURES / "yield_corpus"


def _load(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _make_http_client(tmp_path: Path) -> HttpClient:
    cache = ResponseCache(tmp_path / "cache")
    limiter = RateLimiter(requests_per_second=1000.0, burst=1000, global_concurrency=1000)
    robots = RobotsChecker(user_agent=USER_AGENT)
    return HttpClient(cache=cache, limiter=limiter, robots=robots, user_agent=USER_AGENT)


def _mock_site(base: str, pages: dict[str, tuple[int, str]], *, max_fetches: int = 12) -> None:
    """Explicitly mock every URL the resolver's crawl will actually try
    for `base`, defaulting to 404 unless overridden in `pages` (keyed by
    crawl path -- "" for the homepage, "sitemap.xml" for the sitemap
    check).

    respx routes registered with no explicit path (e.g. `respx.get(base)`
    for a bare origin) constrain only scheme+host, not path -- they match
    *every* path on that host, silently shadowing more specific routes
    registered after them. Every URL here gets its own route with an
    explicit path instead, which is what makes respx's default "fail on
    any unmocked request" behaviour actually catch a real mismatch rather
    than a same-length coincidence.
    """
    sitemap_url = f"{base.rstrip('/')}/sitemap.xml"

    def _url_for(key: str) -> str:
        if key == "sitemap.xml":
            return sitemap_url
        return f"{base}/{key}" if key else f"{base}/"

    overrides = {_url_for(key): value for key, value in pages.items()}

    crawl_urls = select_crawl_urls(base, max_fetches=max_fetches)
    for url in [sitemap_url, *crawl_urls]:
        # select_crawl_urls strips the homepage to the bare origin (no
        # trailing slash); route it explicitly with one, per the respx
        # gotcha above -- httpx normalises the outgoing request the same way.
        route_url = url if url != base else f"{base}/"
        status, body = overrides.get(route_url, (404, "not found"))
        respx.get(route_url).mock(return_value=httpx.Response(status, text=body))

    # Every crawled page goes through respect_robots=True, which fetches
    # /robots.txt once per host and caches the parser -- mock it open
    # (RobotsChecker's own fail-open-on-404 tests live in Session 04).
    respx.get(f"{base.rstrip('/')}/robots.txt").mock(return_value=httpx.Response(404))


def _ctx(*, website: str) -> object:
    from uuid import uuid4

    from app.resolvers.base import LeadContext

    return LeadContext(
        company_id=uuid4(),
        company_name="Acme Dental",
        domain=website.replace("https://", ""),
        website=website,
        country_code="US",
    )


# --------------------------------------------------------------------------
# Email extraction
# --------------------------------------------------------------------------


def test_extracts_mailto_links() -> None:
    html = _load(FIXTURES / "contact_mailto_and_jsonld_basecamp.html")
    hits = extract_emails(html, "https://basecamp.com/about")

    mailto_hits = [h for h in hits if h.strategy == "mailto"]
    assert any(h.value == "jason@basecamp.com" for h in mailto_hits)


def test_extracts_plain_text_emails() -> None:
    html = _load(FIXTURES / "impressum_de_mittelstands_agentur.html")
    hits = extract_emails(html, "https://mittelstands-agentur.de/impressum/")

    plain_hits = [h for h in hits if h.strategy == "plain_text"]
    assert any(h.value == "service@mittelstands-agentur.de" for h in plain_hits)


def test_extracts_at_bracket_obfuscation() -> None:
    html = _load(SYNTHETIC / "obfuscated_email_bracket_at.html")
    hits = extract_emails(html, "https://riversidebooks.example/contact")

    values = {h.value for h in hits}
    assert "info@riversidebooks.com" in values


def test_extracts_paren_at_obfuscation() -> None:
    html = _load(SYNTHETIC / "obfuscated_email_bracket_at.html")
    hits = extract_emails(html, "https://riversidebooks.example/contact")

    values = {h.value for h in hits}
    assert "events@riversidebooks.com" in values


def test_extracts_html_entity_encoded() -> None:
    html = _load(SYNTHETIC / "obfuscated_email_html_entity.html")
    hits = extract_emails(html, "https://riversidebooks.example/contact")

    values = {h.value for h in hits}
    assert "contact@riversidebooks.com" in values


def test_extracts_jsonld_organization_email() -> None:
    html = _load(FIXTURES / "team_names_titles_jsonld_email_discourse.html")
    hits = extract_emails(html, "https://www.discourse.org/team")

    jsonld_hits = {h.value: h for h in hits if h.strategy == "jsonld"}
    assert "team@discourse.org" in jsonld_hits
    assert jsonld_hits["team@discourse.org"].confidence == pytest.approx(0.95)


def test_email_malformed_jsonld_script_ignored_not_crashed() -> None:
    html = (
        '<html><head><script type="application/ld+json">{not valid json</script></head>'
        '<body><a href="mailto:info@riversidebooks.com">Email</a></body></html>'
    )
    hits = extract_emails(html, "https://riversidebooks.example")

    # The broken JSON-LD block is silently skipped, but the rest of the
    # page still extracts normally -- one malformed script must not sink
    # every other strategy.
    assert any(h.value == "info@riversidebooks.com" for h in hits)


def test_mailto_and_obfuscated_bad_hints_filtered() -> None:
    html = (
        "<html><body>"
        '<a href="mailto:noreply@sentry.io">Errors</a>'
        "<p>Reach support at admin [at] wixpress [dot] com any time.</p>"
        "</body></html>"
    )
    hits = extract_emails(html, "https://example.invalid")

    values = {h.value for h in hits}
    assert "noreply@sentry.io" not in values
    assert "admin@wixpress.com" not in values


def test_html_entity_run_that_is_not_an_email_ignored() -> None:
    # A long run of numeric character references that decodes to
    # something that is not an email address at all.
    html = (
        "<html><body><p>&#72;&#101;&#108;&#108;&#111;&#32;&#116;&#104;&#101;&#114;&#101;</p>"
        "</body></html>"
    )
    hits = extract_emails(html, "https://example.invalid")

    assert hits == []


def test_rejects_image_and_vendor_noise() -> None:
    html = _load(SYNTHETIC / "vendor_noise.html")
    hits = extract_emails(html, "https://example.invalid/noise")

    values = {h.value for h in hits}
    assert "noreply@sentry.io" not in values
    assert "admin@wixpress.com" not in values
    assert "someone@example.com" not in values
    assert "user@yourdomain.com" not in values
    # The one real address on the page must still survive the filter.
    assert "hello@riversidebooks.com" in values


@pytest.mark.parametrize("prefix", sorted(ROLE_PREFIXES))
def test_classifies_role_vs_personal(prefix: str) -> None:
    html = f'<html><body><a href="mailto:{prefix}@example.org">Email</a></body></html>'
    hits = extract_emails(html, "https://example.org/contact")

    hit = next(h for h in hits if h.value == f"{prefix}@example.org")
    assert hit.is_role_account is True


def test_classifies_personal_address_as_not_role() -> None:
    html = '<html><body><a href="mailto:jane.doe@example.org">Email</a></body></html>'
    hits = extract_emails(html, "https://example.org/team")

    assert hits[0].is_role_account is False


# --------------------------------------------------------------------------
# Person extraction
# --------------------------------------------------------------------------


def test_extracts_jsonld_founder_and_employee() -> None:
    zapier_html = _load(FIXTURES / "jsonld_organization_founders_zapier.html")
    buffer_html = _load(FIXTURES / "jsonld_organization_founder_buffer.html")

    zapier_people = extract_people(zapier_html, "https://zapier.com/about")
    buffer_people = extract_people(buffer_html, "https://buffer.com/about")

    zapier_names = {p.name for p in zapier_people if p.strategy == "jsonld"}
    assert {"Bryan Helmig", "Wade Foster", "Mike Knoop"} <= zapier_names

    buffer_names = {p.name for p in buffer_people if p.strategy == "jsonld"}
    assert "Joel Gascoigne" in buffer_names


def test_extracts_impressum_geschaeftsfuehrer() -> None:
    ma_html = _load(FIXTURES / "impressum_de_mittelstands_agentur.html")
    mn_html = _load(FIXTURES / "impressum_de_mittelstand_nachrichten.html")

    ma_people = extract_people(ma_html, "https://mittelstands-agentur.de/impressum/")
    mn_people = extract_people(mn_html, "https://www.mittelstand-nachrichten.de/impressum/")

    assert any(p.name == "Stefan Müller" and p.strategy == "impressum" for p in ma_people)
    assert any(p.name == "Sven Oliver Rüsche" and p.strategy == "impressum" for p in mn_people)


def test_extracts_impressum_vertreten_durch_and_inhaber() -> None:
    html = _load(SYNTHETIC / "impressum_variants.html")
    people = extract_people(html, "https://example.invalid/impressum")

    names = {p.name for p in people}
    assert "Anna Fischer" in names  # "Vertreten durch"
    assert "Thomas Weber" in names  # "Inhaber"


def test_extracts_credentialed_name() -> None:
    html = _load(SYNTHETIC / "credentialed_name.html")
    people = extract_people(html, "https://example.invalid/team")

    credentialed = [p for p in people if p.strategy == "credentialed"]
    assert any(p.name == "Jane Smith" for p in credentialed)


def test_person_malformed_jsonld_script_ignored_not_crashed() -> None:
    html = (
        '<html><head><script type="application/ld+json">{not valid json</script></head>'
        "<body><div>Jane Doe</div><div>Owner</div></body></html>"
    )
    people = extract_people(html, "https://example.invalid")

    assert any(p.name == "Jane Doe" for p in people)


def test_extracts_byline_founded_by() -> None:
    html = "<html><body><p>Riverside Books was Founded by Clara Nguyen in 1998.</p></body></html>"
    people = extract_people(html, "https://riversidebooks.example/about")

    byline_hits = [p for p in people if p.strategy == "byline"]
    assert any(p.name == "Clara Nguyen" for p in byline_hits)


def test_impressum_pattern_match_rejected_when_not_name_shaped() -> None:
    # The regex matches two capitalised words here ("Support Team"), but
    # "team" is a title/role word, not a person -- _looks_like_name must
    # still reject it even though the shape matched.
    html = "<html><body><p>Geschäftsführer Support Team</p></body></html>"
    people = extract_people(html, "https://example.invalid/impressum")

    assert people == []


def test_credentialed_and_dr_prefix_reject_non_name_candidates() -> None:
    # Both lines are shape-valid (two capitalised words) so the regexes
    # do match, but "Our Team" is a section-heading phrase, not a person
    # -- _looks_like_name must still reject it.
    html = (
        "<html><body>"
        "<p>Contact Our Team, MD for details.</p>"
        "<p>Ask Dr. Our Team for a quote.</p>"
        "</body></html>"
    )
    people = extract_people(html, "https://example.invalid")

    assert people == []


def test_extracts_heading_adjacent_title() -> None:
    """Non-medical generalisation: a real name/title pair from a software
    company's team page, not a dental credential in sight.
    """
    html = _load(FIXTURES / "team_names_titles_jsonld_email_discourse.html")
    people = extract_people(html, "https://www.discourse.org/team")

    by_name = {p.name: p for p in people if p.strategy == "heading_adjacent"}
    assert "Jeff Atwood" in by_name
    assert by_name["Jeff Atwood"].title == "Executive Chairman"


def test_rejects_section_headings_as_names() -> None:
    html = _load(SYNTHETIC / "testimonial_and_section_heading_noise.html")
    people = extract_people(html, "https://riversidebooks.example/about")

    names = {p.name.lower() for p in people}
    assert "our team" not in names


def test_rejects_testimonial_authors() -> None:
    html = _load(SYNTHETIC / "testimonial_and_section_heading_noise.html")
    people = extract_people(html, "https://riversidebooks.example/about")

    names = {p.name for p in people}
    assert "Amanda Torres" not in names
    assert "Marcus Webb" not in names


@pytest.mark.parametrize(
    ("fixture", "name", "expected_role_class"),
    [
        ("team_names_titles_jsonld_email_discourse.html", "Jeff Atwood", "owner"),
        ("team_names_titles_jsonld_email_discourse.html", "Glenn Toh", "manager"),
        ("team_names_titles_jsonld_email_discourse.html", "Mae Woods", "marketing"),
        ("impressum_de_mittelstands_agentur.html", "Stefan Müller", "owner"),
    ],
)
def test_assigns_role_class_from_title(fixture: str, name: str, expected_role_class: str) -> None:
    html = _load(FIXTURES / fixture)
    people = extract_people(html, f"https://example.com/{fixture}")

    match = next(p for p in people if p.name == name)
    assert match.role_class.value == expected_role_class


# --------------------------------------------------------------------------
# crawl_paths
# --------------------------------------------------------------------------


def test_uses_sitemap_when_present() -> None:
    sitemap_urls = [
        "https://acme.example.com/contact",
        "https://acme.example.com/team",
    ]

    urls = select_crawl_urls("https://acme.example.com", sitemap_urls=sitemap_urls, max_fetches=5)

    # Sitemap-confirmed paths come first, so a limited fetch budget is
    # spent on pages known to exist before any blind guessing.
    assert urls[0] in sitemap_urls
    assert urls[1] in sitemap_urls


def test_select_crawl_urls_dedupes_and_caps() -> None:
    urls = select_crawl_urls("https://acme.example.com", max_fetches=6)

    assert len(urls) == 6
    assert len(urls) == len(set(urls))  # "team" appears twice in CRAWL_PATHS


def test_parse_sitemap_extracts_loc_urls() -> None:
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>https://acme.example.com/contact</loc></url>"
        "<url><loc>https://acme.example.com/team</loc></url>"
        "</urlset>"
    )
    urls = parse_sitemap(xml)

    assert urls == ["https://acme.example.com/contact", "https://acme.example.com/team"]


def test_parse_sitemap_malformed_xml_returns_empty_list() -> None:
    assert parse_sitemap("<not valid xml") == []


def test_parse_sitemap_falls_back_when_namespace_missing() -> None:
    xml = "<urlset><url><loc>https://acme.example.com/about</loc></url></urlset>"

    assert parse_sitemap(xml) == ["https://acme.example.com/about"]


# --------------------------------------------------------------------------
# WebsiteEmailResolver / WebsitePersonResolver
# --------------------------------------------------------------------------


@respx.mock
async def test_does_not_stop_at_first_hit(tmp_path: Path) -> None:
    """The prototype's exact bug: it stopped crawling once it had one
    email and one name, so a generic info@ on the homepage suppressed the
    real owner's address found later on /team. This resolver must not.
    """
    base = "https://acmedental.test"
    homepage_html = (
        '<html><body><footer><a href="mailto:info@acmedental.test">Email</a></footer></body></html>'
    )
    team_html = (
        "<html><body>"
        "<div>Jane Doe</div><div>Owner</div>"
        '<a href="mailto:jane@acmedental.test">Jane</a>'
        "</body></html>"
    )
    _mock_site(
        base,
        {
            "sitemap.xml": (404, ""),
            "": (200, homepage_html),
            "team": (200, team_html),
        },
    )

    http = _make_http_client(tmp_path)
    try:
        ctx = _ctx(website=base)
        email_candidates = await WebsiteEmailResolver(http=http).resolve(ctx)  # type: ignore[arg-type]
        person_candidates = await WebsitePersonResolver(http=http).resolve(ctx)  # type: ignore[arg-type]
    finally:
        await http.aclose()

    email_values = {c.value for c in email_candidates}
    assert "info@acmedental.test" in email_values
    assert "jane@acmedental.test" in email_values

    person_values = {c.value for c in person_candidates}
    assert "Jane Doe" in person_values


@respx.mock
async def test_escalates_to_browser_only_for_spa(tmp_path: Path) -> None:
    base = "https://spa-example.test"
    static_html = (
        "<html><body><h1>Real Company</h1><p>"
        + ("Lorem ipsum dolor sit. " * 20)
        + "</p></body></html>"
    )
    spa_shell_html = (
        "<html><head>"
        + "".join(f'<script src="/a{i}.js"></script>' for i in range(6))
        + '</head><body><div id="root"></div></body></html>'
    )
    rendered_html = "<html><body><div>Real Person</div><div>Owner</div></body></html>"

    _mock_site(
        base,
        {
            "sitemap.xml": (404, ""),
            "": (200, static_html),
            "team": (200, spa_shell_html),
        },
    )

    class FakeBrowser:
        def __init__(self) -> None:
            self.call_count = 0
            self.fetched_urls: list[str] = []

        async def fetch(self, url: str, **kwargs: object) -> str:
            self.call_count += 1
            self.fetched_urls.append(url)
            return rendered_html

        async def start(self) -> None:  # pragma: no cover - not exercised here
            pass

        async def stop(self) -> None:  # pragma: no cover - not exercised here
            pass

    browser = FakeBrowser()
    http = _make_http_client(tmp_path)
    try:
        resolver = WebsitePersonResolver(http=http, browser=browser)  # type: ignore[arg-type]
        await resolver.resolve(_ctx(website=base))  # type: ignore[arg-type]
    finally:
        await http.aclose()

    assert browser.call_count == 1
    assert browser.fetched_urls == [f"{base}/team"]


@respx.mock
async def test_every_hit_carries_source_url(tmp_path: Path) -> None:
    base = "https://acmedental.test"
    homepage_html = '<html><body><a href="mailto:info@acmedental.test">Email</a></body></html>'
    _mock_site(base, {"sitemap.xml": (404, ""), "": (200, homepage_html)})

    http = _make_http_client(tmp_path)
    try:
        candidates = await WebsiteEmailResolver(http=http).resolve(_ctx(website=base))  # type: ignore[arg-type]
    finally:
        await http.aclose()

    assert len(candidates) > 0
    assert all(c.source_url for c in candidates)


async def test_resolver_returns_empty_when_lead_has_no_website(tmp_path: Path) -> None:
    ctx = _ctx(website="")
    http = _make_http_client(tmp_path)  # never touched -- no website means no fetch at all
    try:
        candidates = await WebsiteEmailResolver(http=http).resolve(ctx)  # type: ignore[arg-type]
    finally:
        await http.aclose()

    assert candidates == []


@respx.mock
async def test_non_200_sitemap_and_page_responses_degrade_gracefully(tmp_path: Path) -> None:
    """A sitemap or a crawl path that responds with something other than
    200 or a raised error (e.g. a 3xx httpx doesn't follow) must be
    skipped, not mistaken for real content.
    """
    base = "https://acmedental.test"
    homepage_html = '<html><body><a href="mailto:info@acmedental.test">Email</a></body></html>'
    sitemap_url = f"{base}/sitemap.xml"

    respx.get(sitemap_url).mock(return_value=httpx.Response(204))  # no content, not an error
    respx.get(f"{base}/robots.txt").mock(return_value=httpx.Response(404))
    crawl_urls = select_crawl_urls(base, max_fetches=12)
    for url in crawl_urls:
        route_url = url if url != base else f"{base}/"
        if route_url == f"{base}/":
            respx.get(route_url).mock(return_value=httpx.Response(200, text=homepage_html))
        elif route_url == f"{base}/contact":
            respx.get(route_url).mock(return_value=httpx.Response(301, headers={"Location": "/"}))
        else:
            respx.get(route_url).mock(return_value=httpx.Response(404, text="not found"))

    http = _make_http_client(tmp_path)
    try:
        candidates = await WebsiteEmailResolver(http=http).resolve(_ctx(website=base))  # type: ignore[arg-type]
    finally:
        await http.aclose()

    # The homepage's real hit still comes through; the 204 sitemap and the
    # unfollowed 301 are skipped cleanly rather than crashing the crawl.
    assert any(c.value == "info@acmedental.test" for c in candidates)


# --------------------------------------------------------------------------
# Yield floors — the canaries
# --------------------------------------------------------------------------

_PAGE_SUFFIXES = ("_about", "_team", "_contact", "_press", "_home", "_impressum", "_newsroom")


def _company_key(path: Path) -> str:
    stem = path.stem
    for suffix in _PAGE_SUFFIXES:
        if stem.endswith(suffix):
            return stem[: -len(suffix)]
    return stem


def _yield_by_company() -> dict[str, dict[str, bool]]:
    """Group the corpus by company (some companies have more than one
    fetched page, e.g. an about page and a press page) and check whether
    *any* of that company's pages yielded a hit — matching how the real
    resolver works: it crawls multiple pages per site and aggregates.
    """
    companies: dict[str, dict[str, bool]] = {}
    for path in sorted(YIELD_CORPUS.glob("*.html")):
        key = _company_key(path)
        html = path.read_text(encoding="utf-8")
        has_email = bool(extract_emails(html, f"https://example.com/{path.name}"))
        has_person = bool(extract_people(html, f"https://example.com/{path.name}"))
        entry = companies.setdefault(key, {"email": False, "person": False})
        entry["email"] = entry["email"] or has_email
        entry["person"] = entry["person"] or has_person
    return companies


def test_crawler_email_yield_floor() -> None:
    companies = _yield_by_company()
    total = len(companies)
    hits = sum(1 for v in companies.values() if v["email"])
    rate = hits / total

    assert rate >= 0.50, f"email yield {rate:.0%} ({hits}/{total}) below the 50% floor"


def test_crawler_person_yield_floor() -> None:
    companies = _yield_by_company()
    total = len(companies)
    hits = sum(1 for v in companies.values() if v["person"])
    rate = hits / total

    assert rate >= 0.30, f"person yield {rate:.0%} ({hits}/{total}) below the 30% floor"
