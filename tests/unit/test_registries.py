"""Unit tests for company registries: Companies House, OpenCorporates,
EDGAR, the shared fuzzy-match/name-parsing helpers, and the coverage map.
Network is always mocked with respx; fixtures under
tests/fixtures/json/{companies_house,opencorporates,edgar}/.

Test 13 (test_us_lead_never_calls_companies_house) is the load-bearing one
in this file — it protects both correctness (a wrong-country match
attributes a stranger's name to a business) and cost (a jurisdiction miss
on a paid tier is a wasted call). Test 19 is the yield canary: the number
this whole session exists to move.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import respx

from app.db.models.enums import RoleClass
from app.net.cache import ResponseCache
from app.net.client import HttpClient
from app.net.ratelimit import RateLimiter
from app.net.robots import RobotsChecker
from app.resolvers.base import LeadContext, Tier
from app.resolvers.executor import run_waterfall
from app.resolvers.person.registries.base import (
    MATCH_THRESHOLD,
    CompanyQuery,
    RegistryCompany,
    parse_officer_name,
    score_company_match,
)
from app.resolvers.person.registries.companies_house import (
    CompaniesHouseResolver,
    _extract_postcode,
)
from app.resolvers.person.registries.coverage import (
    COVERAGE,
    has_free_registry,
    registries_for,
)
from app.resolvers.person.registries.edgar import EdgarResolver
from app.resolvers.person.registries.opencorporates import (
    OpenCorporatesResolver,
    _postcode_from,
    _role_class_from_position,
)
from app.resolvers.registry import build_registry

USER_AGENT = "EmailMarketingToolBot/1.0 (+https://example.invalid/bot)"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "json"

CH_SEARCH_RE = r"https://api\.company-information\.service\.gov\.uk/search/companies.*"
CH_OFFICERS_RE = r"https://api\.company-information\.service\.gov\.uk/company/.*/officers"
OC_SEARCH_RE = r"https://api\.opencorporates\.com/v0\.4/companies/search.*"
OC_OFFICERS_RE = r"https://api\.opencorporates\.com/v0\.4/companies/.*/officers.*"
EDGAR_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
EDGAR_SUBMISSIONS_RE = r"https://data\.sec\.gov/submissions/CIK.*\.json"
EDGAR_ARCHIVE_RE = r"https://www\.sec\.gov/Archives/edgar/data/.*"


async def _no_sleep(delay: float) -> None:
    return None


def _load_json(*parts: str) -> dict:
    return json.loads((FIXTURES / Path(*parts)).read_text(encoding="utf-8"))


def _load_text(*parts: str) -> str:
    return (FIXTURES / Path(*parts)).read_text(encoding="utf-8")


def _make_http_client(tmp_path: Path, *, sleep: object = None) -> HttpClient:
    cache = ResponseCache(tmp_path / "cache")
    limiter = RateLimiter(requests_per_second=1000.0, burst=1000, global_concurrency=1000)
    robots = RobotsChecker(user_agent=USER_AGENT)
    return HttpClient(
        cache=cache,
        limiter=limiter,
        robots=robots,
        user_agent=USER_AGENT,
        sleep=sleep or _no_sleep,  # type: ignore[arg-type]
    )


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


# --------------------------------------------------------------------------
# score_company_match / parse_officer_name — the shared helpers
# --------------------------------------------------------------------------


def test_search_parses_company_results() -> None:
    data = _load_json("companies_house", "search_northgate.json")
    items = data["items"]
    assert len(items) == 2
    assert items[0]["company_number"] == "09876543"
    assert items[0]["title"] == "NORTHGATE DENTAL CARE LIMITED"


def test_match_below_threshold_is_rejected() -> None:
    query = CompanyQuery(name="Northgate Dental", country_code="GB")
    candidate = RegistryCompany(
        company_number="05554443", name="RIVERSIDE MEDICAL CENTRE LIMITED", entity_type="ltd"
    )
    match = score_company_match(query, candidate)
    assert match.match_score < MATCH_THRESHOLD


def test_postcode_agreement_boosts_match_score() -> None:
    query_no_postcode = CompanyQuery(name="Northgate Dental", country_code="GB")
    query_with_postcode = CompanyQuery(
        name="Northgate Dental", country_code="GB", postcode="EC1A 1BB"
    )
    candidate = RegistryCompany(
        company_number="07778889",
        name="NORTHGATE DENTAL PRACTICE LIMITED",
        entity_type="ltd",
        postcode="EC1A1BB",  # registries often omit the space; must still agree
    )

    without = score_company_match(query_no_postcode, candidate)
    with_postcode = score_company_match(query_with_postcode, candidate)

    assert without.match_score < MATCH_THRESHOLD, "name alone must not already clear the floor"
    assert with_postcode.match_score >= MATCH_THRESHOLD
    assert "postcode" in with_postcode.matched_on
    assert "postcode" not in without.matched_on


def test_legal_suffix_ignored_in_matching() -> None:
    query = CompanyQuery(name="Northgate Dental Care", country_code="GB")
    candidate = RegistryCompany(
        company_number="09876543", name="NORTHGATE DENTAL CARE LIMITED", entity_type="ltd"
    )
    match = score_company_match(query, candidate)
    assert match.match_score == pytest.approx(1.0)
    assert "name" in match.matched_on


def test_parses_surname_firstname_format() -> None:
    name = parse_officer_name("SMITH, Jane Elizabeth")
    assert name.first == "Jane"
    assert name.middle == "Elizabeth"
    assert name.last == "Smith"
    assert name.full == "Jane Elizabeth Smith"


def test_handles_single_word_and_multi_barrel_names() -> None:
    obrien = parse_officer_name("O'BRIEN, Sean")
    assert obrien.last == "O'Brien"
    assert obrien.full == "Sean O'Brien"

    van_der_berg = parse_officer_name("VAN DER BERG, Johannes Willem")
    assert van_der_berg.last == "Van der Berg"
    assert van_der_berg.first == "Johannes"
    assert van_der_berg.middle == "Willem"

    hyphenated = parse_officer_name("SMITH-JONES, Anna")
    assert hyphenated.last == "Smith-Jones"


def test_parse_officer_name_natural_order_no_comma() -> None:
    """OpenCorporates and most non-UK registries hand back "Forename
    Surname" with no comma — the same parser must handle both shapes.
    """
    name = parse_officer_name("Jane Elizabeth Smith")
    assert name.first == "Jane"
    assert name.middle == "Elizabeth"
    assert name.last == "Smith"


def test_parse_officer_name_single_word_has_no_forename() -> None:
    name = parse_officer_name("Cher")
    assert name.first == ""
    assert name.last == "Cher"


# --------------------------------------------------------------------------
# Companies House
# --------------------------------------------------------------------------


@respx.mock
async def test_officers_parses_active_directors(tmp_path: Path) -> None:
    respx.get(url__regex=CH_SEARCH_RE).mock(
        return_value=httpx.Response(
            200, json=_load_json("companies_house", "search_northgate.json")
        )
    )
    respx.get(url__regex=CH_OFFICERS_RE).mock(
        return_value=httpx.Response(
            200, json=_load_json("companies_house", "officers_multiple.json")
        )
    )
    http = _make_http_client(tmp_path)
    try:
        resolver = CompaniesHouseResolver(http=http, api_key="testkey")
        candidates = await resolver.resolve(_ctx(company_name="Northgate Dental Care"))
    finally:
        await http.aclose()

    names = {c.value for c in candidates}
    assert "Sean O'Brien" in names
    assert "Anita Kaur Patel" in names


@respx.mock
async def test_excludes_resigned_officers(tmp_path: Path) -> None:
    respx.get(url__regex=CH_SEARCH_RE).mock(
        return_value=httpx.Response(
            200, json=_load_json("companies_house", "search_northgate.json")
        )
    )
    respx.get(url__regex=CH_OFFICERS_RE).mock(
        return_value=httpx.Response(
            200, json=_load_json("companies_house", "officers_sole_director.json")
        )
    )
    http = _make_http_client(tmp_path)
    try:
        resolver = CompaniesHouseResolver(http=http, api_key="testkey")
        candidates = await resolver.resolve(_ctx(company_name="Northgate Dental Care"))
    finally:
        await http.aclose()

    names = {c.value for c in candidates}
    assert names == {"Jane Elizabeth Smith"}  # Robert Jones resigned, excluded


@respx.mock
async def test_sole_director_gets_higher_confidence(tmp_path: Path) -> None:
    respx.get(url__regex=CH_SEARCH_RE).mock(
        return_value=httpx.Response(
            200, json=_load_json("companies_house", "search_northgate.json")
        )
    )
    respx.get(url__regex=CH_OFFICERS_RE).mock(
        return_value=httpx.Response(
            200, json=_load_json("companies_house", "officers_sole_director.json")
        )
    )
    http = _make_http_client(tmp_path)
    try:
        resolver = CompaniesHouseResolver(http=http, api_key="testkey")
        sole = await resolver.resolve(_ctx(company_name="Northgate Dental Care"))
    finally:
        await http.aclose()
    assert sole[0].confidence == pytest.approx(0.90)


@respx.mock
async def test_multiple_directors_get_lower_confidence(tmp_path: Path) -> None:
    respx.get(url__regex=CH_SEARCH_RE).mock(
        return_value=httpx.Response(
            200, json=_load_json("companies_house", "search_northgate.json")
        )
    )
    respx.get(url__regex=CH_OFFICERS_RE).mock(
        return_value=httpx.Response(
            200, json=_load_json("companies_house", "officers_multiple.json")
        )
    )
    http = _make_http_client(tmp_path)
    try:
        resolver = CompaniesHouseResolver(http=http, api_key="testkey")
        candidates = await resolver.resolve(_ctx(company_name="Northgate Dental Care"))
    finally:
        await http.aclose()

    directors = {c.value: c.confidence for c in candidates if c.extra["officer_role"] == "director"}
    assert directors == {
        "Sean O'Brien": pytest.approx(0.75),
        "Anita Kaur Patel": pytest.approx(0.75),
    }


@respx.mock
async def test_secretary_maps_to_other_role_class(tmp_path: Path) -> None:
    respx.get(url__regex=CH_SEARCH_RE).mock(
        return_value=httpx.Response(
            200, json=_load_json("companies_house", "search_northgate.json")
        )
    )
    respx.get(url__regex=CH_OFFICERS_RE).mock(
        return_value=httpx.Response(
            200, json=_load_json("companies_house", "officers_multiple.json")
        )
    )
    http = _make_http_client(tmp_path)
    try:
        resolver = CompaniesHouseResolver(http=http, api_key="testkey")
        candidates = await resolver.resolve(_ctx(company_name="Northgate Dental Care"))
    finally:
        await http.aclose()

    secretary = next(c for c in candidates if c.value == "Susan Wright")
    assert secretary.extra["role_class"] == RoleClass.OTHER.value
    assert secretary.confidence == pytest.approx(0.50)


@respx.mock
async def test_nominee_director_low_confidence(tmp_path: Path) -> None:
    respx.get(url__regex=CH_SEARCH_RE).mock(
        return_value=httpx.Response(
            200, json=_load_json("companies_house", "search_northgate.json")
        )
    )
    respx.get(url__regex=CH_OFFICERS_RE).mock(
        return_value=httpx.Response(
            200, json=_load_json("companies_house", "officers_multiple.json")
        )
    )
    http = _make_http_client(tmp_path)
    try:
        resolver = CompaniesHouseResolver(http=http, api_key="testkey")
        candidates = await resolver.resolve(_ctx(company_name="Northgate Dental Care"))
    finally:
        await http.aclose()

    nominee = next(c for c in candidates if c.value == "David Davies")
    assert nominee.extra["role_class"] == RoleClass.OTHER.value
    assert nominee.confidence == pytest.approx(0.30)


@respx.mock
async def test_corporate_director_filtered_out(tmp_path: Path) -> None:
    """Companies House lists corporate directors (another company as
    director) — filtered entirely, since they have no personal email.
    """
    respx.get(url__regex=CH_SEARCH_RE).mock(
        return_value=httpx.Response(
            200, json=_load_json("companies_house", "search_northgate.json")
        )
    )
    respx.get(url__regex=CH_OFFICERS_RE).mock(
        return_value=httpx.Response(
            200, json=_load_json("companies_house", "officers_multiple.json")
        )
    )
    http = _make_http_client(tmp_path)
    try:
        resolver = CompaniesHouseResolver(http=http, api_key="testkey")
        candidates = await resolver.resolve(_ctx(company_name="Northgate Dental Care"))
    finally:
        await http.aclose()

    assert "NOMINEE DIRECTOR SERVICES LIMITED" not in {c.value for c in candidates}
    assert all("Limited" not in c.value for c in candidates)


@respx.mock
async def test_llp_designated_member_maps_to_owner(tmp_path: Path) -> None:
    respx.get(url__regex=CH_SEARCH_RE).mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "company_number": "11112222",
                        "title": "VAN DER BERG PARTNERS LLP",
                        "company_type": "llp",
                        "address_snippet": "1 Legal Sq, London, EC2A 2AA",
                    }
                ]
            },
        )
    )
    respx.get(url__regex=CH_OFFICERS_RE).mock(
        return_value=httpx.Response(200, json=_load_json("companies_house", "officers_llp.json"))
    )
    http = _make_http_client(tmp_path)
    try:
        resolver = CompaniesHouseResolver(http=http, api_key="testkey")
        candidates = await resolver.resolve(_ctx(company_name="van der Berg Partners"))
    finally:
        await http.aclose()

    assert len(candidates) == 1
    assert candidates[0].value == "Johannes Willem Van der Berg"
    assert candidates[0].extra["role_class"] == RoleClass.OWNER.value
    assert candidates[0].confidence == pytest.approx(0.85)


@respx.mock
async def test_entity_type_recorded_as_fact(tmp_path: Path) -> None:
    """entity_type flows onto every candidate's extra -- the PECR gate
    (corporate subscribers exempt from consent, sole traders not) needs it.
    """
    respx.get(url__regex=CH_SEARCH_RE).mock(
        return_value=httpx.Response(
            200, json=_load_json("companies_house", "search_northgate.json")
        )
    )
    respx.get(url__regex=CH_OFFICERS_RE).mock(
        return_value=httpx.Response(
            200, json=_load_json("companies_house", "officers_sole_director.json")
        )
    )
    http = _make_http_client(tmp_path)
    try:
        resolver = CompaniesHouseResolver(http=http, api_key="testkey")
        candidates = await resolver.resolve(_ctx(company_name="Northgate Dental Care"))
    finally:
        await http.aclose()

    assert candidates[0].extra["entity_type"] == "ltd"


@respx.mock
async def test_us_lead_never_calls_companies_house(tmp_path: Path) -> None:
    """Jurisdiction is a hard gate — protects both correctness (a US
    company can't be matched against the UK register) and cost.
    """
    route = respx.get(url__regex=CH_SEARCH_RE).mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    officers_route = respx.get(url__regex=CH_OFFICERS_RE).mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    http = _make_http_client(tmp_path)
    try:
        resolver = CompaniesHouseResolver(http=http, api_key="testkey")
        registry = build_registry([resolver])

        resolution = await run_waterfall(
            field="person_name", ctx=_ctx(country_code="US"), registry=registry, threshold=0.85
        )
    finally:
        await http.aclose()

    assert resolution.best is None
    assert route.call_count == 0
    assert officers_route.call_count == 0


@respx.mock
async def test_gb_lead_does_call_companies_house(tmp_path: Path) -> None:
    route = respx.get(url__regex=CH_SEARCH_RE).mock(
        return_value=httpx.Response(
            200, json=_load_json("companies_house", "search_northgate.json")
        )
    )
    respx.get(url__regex=CH_OFFICERS_RE).mock(
        return_value=httpx.Response(
            200, json=_load_json("companies_house", "officers_sole_director.json")
        )
    )
    http = _make_http_client(tmp_path)
    try:
        resolver = CompaniesHouseResolver(http=http, api_key="testkey")
        registry = build_registry([resolver])

        resolution = await run_waterfall(
            field="person_name",
            ctx=_ctx(company_name="Northgate Dental Care", country_code="GB"),
            registry=registry,
            threshold=0.85,
        )
    finally:
        await http.aclose()

    assert route.call_count == 1
    assert resolution.best is not None
    assert resolution.best.value == "Jane Elizabeth Smith"


@respx.mock
async def test_auth_header_is_basic_with_api_key_as_username(tmp_path: Path) -> None:
    captured: dict[str, str] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"items": []})

    respx.get(url__regex=CH_SEARCH_RE).mock(side_effect=_capture)
    http = _make_http_client(tmp_path)
    try:
        resolver = CompaniesHouseResolver(http=http, api_key="my-secret-key")
        await resolver.resolve(_ctx())
    finally:
        await http.aclose()

    expected = "Basic " + base64.b64encode(b"my-secret-key:").decode()
    assert captured["authorization"] == expected


@respx.mock
async def test_rate_limit_429_backs_off(tmp_path: Path) -> None:
    """Companies House resolvers reuse Session 04's HttpClient, so a 429
    must be retried and backed off exactly the way test_http_client.py
    already proves for the client in isolation — this just checks the
    resolver doesn't bypass that path.
    """
    route = respx.get(url__regex=CH_SEARCH_RE)
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(200, json=_load_json("companies_house", "search_northgate.json")),
    ]
    respx.get(url__regex=CH_OFFICERS_RE).mock(
        return_value=httpx.Response(
            200, json=_load_json("companies_house", "officers_sole_director.json")
        )
    )
    http = _make_http_client(tmp_path)
    try:
        resolver = CompaniesHouseResolver(http=http, api_key="testkey")
        candidates = await resolver.resolve(_ctx(company_name="Northgate Dental Care"))
    finally:
        await http.aclose()

    assert route.call_count == 2
    assert candidates  # eventually succeeded despite the 429


@respx.mock
async def test_registry_yield_floor_for_gb(tmp_path: Path) -> None:
    """>=70% of 20 fixture UK companies yield a named officer. Includes
    genuine miss cases (no match, no active officers, corporate-only
    officers) rather than an inflated 100% — see Session 06's yield tests
    for why an honest floor matters more than a clean number.
    """
    corpus = _load_json("companies_house", "yield_corpus.json")
    if isinstance(corpus, dict):
        corpus = corpus["items"] if "items" in corpus else list(corpus.values())

    by_number: dict[str, dict] = {}
    for entry in corpus:
        for item in entry["search_response"]["items"]:
            by_number[item["company_number"]] = entry
    by_query = {entry["query_name"]: entry for entry in corpus}

    def _search_side_effect(request: httpx.Request) -> httpx.Response:
        q = request.url.params["q"]
        return httpx.Response(200, json=by_query[q]["search_response"])

    def _officers_side_effect(request: httpx.Request) -> httpx.Response:
        number = request.url.path.split("/")[2]
        entry = by_number.get(number)
        officers = (entry or {}).get("officers_response") or {"items": []}
        return httpx.Response(200, json=officers)

    respx.get(url__regex=CH_SEARCH_RE).mock(side_effect=_search_side_effect)
    respx.get(url__regex=CH_OFFICERS_RE).mock(side_effect=_officers_side_effect)

    http = _make_http_client(tmp_path)
    hits = 0
    try:
        resolver = CompaniesHouseResolver(http=http, api_key="testkey")
        for entry in corpus:
            candidates = await resolver.resolve(_ctx(company_name=entry["query_name"]))
            if candidates:
                hits += 1
    finally:
        await http.aclose()

    yield_rate = hits / len(corpus)
    assert yield_rate >= 0.70, f"GB registry yield {yield_rate:.1%} below the 70% floor"


# --------------------------------------------------------------------------
# OpenCorporates
# --------------------------------------------------------------------------


async def test_opencorporates_skipped_without_key(tmp_path: Path) -> None:
    """Degrades explicitly, does not crash: no key configured -> not
    applicable, SKIPPED in telemetry, never attempted.
    """
    http = _make_http_client(tmp_path)
    try:
        resolver = OpenCorporatesResolver(http=http, api_key=None)
        assert await resolver.applicable(_ctx(country_code="AU")) is False
    finally:
        await http.aclose()


@respx.mock
async def test_opencorporates_is_metered_tier(tmp_path: Path) -> None:
    """Only fires when the free tier missed. companies_house is FREE and
    GB-only; for an AU lead there's no free registry at all, so
    OpenCorporates should be the one that actually runs.
    """
    assert OpenCorporatesResolver.tier is Tier.METERED

    oc_route = respx.get(url__regex=OC_SEARCH_RE).mock(
        return_value=httpx.Response(200, json=_load_json("opencorporates", "search_widgets.json"))
    )
    respx.get(url__regex=OC_OFFICERS_RE).mock(
        return_value=httpx.Response(200, json=_load_json("opencorporates", "officers_widgets.json"))
    )
    ch_route = respx.get(url__regex=CH_SEARCH_RE).mock(
        return_value=httpx.Response(200, json={"items": []})
    )

    http = _make_http_client(tmp_path)
    try:
        ch = CompaniesHouseResolver(http=http, api_key="testkey")
        oc = OpenCorporatesResolver(http=http, api_key="oc-key")
        registry = build_registry([ch, oc])

        resolution = await run_waterfall(
            field="person_name",
            ctx=_ctx(company_name="Acme Widgets", country_code="AU"),
            registry=registry,
            # OpenCorporates' fixture officer lands at 0.60 confidence —
            # realistic for a METERED, cross-jurisdiction aggregator. A
            # threshold above that would leave resolution.best unset even
            # though the call correctly happened; 0.5 keeps the assertion
            # about *whether it ran* separate from *how confident it was*.
            threshold=0.5,
            allow_metered=True,
        )
    finally:
        await http.aclose()

    assert ch_route.call_count == 0, "GB-only resolver must never run for an AU lead"
    assert oc_route.call_count == 1
    assert resolution.best is not None
    assert resolution.best.value == "Jane Smith"


@respx.mock
async def test_stops_at_free_tier_without_touching_opencorporates(tmp_path: Path) -> None:
    """The other half of the cost guarantee: when a free registry already
    clears the threshold, OpenCorporates must never be awaited.
    """
    respx.get(url__regex=CH_SEARCH_RE).mock(
        return_value=httpx.Response(
            200, json=_load_json("companies_house", "search_northgate.json")
        )
    )
    respx.get(url__regex=CH_OFFICERS_RE).mock(
        return_value=httpx.Response(
            200, json=_load_json("companies_house", "officers_sole_director.json")
        )
    )
    oc_route = respx.get(url__regex=OC_SEARCH_RE).mock(
        return_value=httpx.Response(200, json={"results": {"companies": []}})
    )

    http = _make_http_client(tmp_path)
    try:
        ch = CompaniesHouseResolver(http=http, api_key="testkey")
        oc = OpenCorporatesResolver(http=http, api_key="oc-key")
        registry = build_registry([ch, oc])

        resolution = await run_waterfall(
            field="person_name",
            ctx=_ctx(company_name="Northgate Dental Care", country_code="GB"),
            registry=registry,
            threshold=0.85,
            allow_metered=True,
        )
    finally:
        await http.aclose()

    assert resolution.stopped_at == Tier.FREE
    assert oc_route.call_count == 0


@respx.mock
async def test_opencorporates_filters_inactive_officers(tmp_path: Path) -> None:
    respx.get(url__regex=OC_SEARCH_RE).mock(
        return_value=httpx.Response(200, json=_load_json("opencorporates", "search_widgets.json"))
    )
    respx.get(url__regex=OC_OFFICERS_RE).mock(
        return_value=httpx.Response(200, json=_load_json("opencorporates", "officers_widgets.json"))
    )
    http = _make_http_client(tmp_path)
    try:
        resolver = OpenCorporatesResolver(http=http, api_key="oc-key")
        candidates = await resolver.resolve(_ctx(company_name="Acme Widgets", country_code="AU"))
    finally:
        await http.aclose()

    names = {c.value for c in candidates}
    assert names == {"Jane Smith"}  # John Doe has an end_date -- inactive


# --------------------------------------------------------------------------
# EDGAR
# --------------------------------------------------------------------------


@respx.mock
async def test_edgar_sets_contact_user_agent(tmp_path: Path) -> None:
    captured: list[str] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        captured.append(request.headers.get("user-agent", ""))
        return httpx.Response(200, json=_load_json("edgar", "company_tickers.json"))

    respx.get(EDGAR_TICKERS_URL).mock(side_effect=_capture)
    empty_filings = {
        "filings": {"recent": {"form": [], "accessionNumber": [], "primaryDocument": []}}
    }
    respx.get(url__regex=EDGAR_SUBMISSIONS_RE).mock(
        return_value=httpx.Response(200, json=empty_filings)
    )

    http = _make_http_client(tmp_path)
    try:
        resolver = EdgarResolver(http=http, contact="EmailMarketingTool research@example.invalid")
        await resolver.resolve(_ctx(company_name="Apple Inc", country_code="US"))
    finally:
        await http.aclose()

    assert captured
    assert "research@example.invalid" in captured[0]


@respx.mock
async def test_edgar_cik_lookup_and_officer_extraction(tmp_path: Path) -> None:
    respx.get(EDGAR_TICKERS_URL).mock(
        return_value=httpx.Response(200, json=_load_json("edgar", "company_tickers.json"))
    )
    respx.get(url__regex=EDGAR_SUBMISSIONS_RE).mock(
        return_value=httpx.Response(200, json=_load_json("edgar", "submissions_aapl.json"))
    )
    respx.get(url__regex=EDGAR_ARCHIVE_RE).mock(
        return_value=httpx.Response(200, text=_load_text("edgar", "form4.xml"))
    )

    http = _make_http_client(tmp_path)
    try:
        resolver = EdgarResolver(http=http, contact="EmailMarketingTool research@example.invalid")
        candidates = await resolver.resolve(_ctx(company_name="Apple Inc", country_code="US"))
    finally:
        await http.aclose()

    by_name = {c.value: c for c in candidates}
    assert by_name["Jane Doe"].extra["role_class"] == RoleClass.OWNER.value  # officer + CEO title
    assert by_name["Michael Carter"].extra["role_class"] == RoleClass.OWNER.value  # director only
    assert "Big Fund Investors LP" not in by_name, "10%-owner-only entries are investors, not staff"


@respx.mock
async def test_edgar_no_recent_insider_filing_degrades_to_empty(tmp_path: Path) -> None:
    respx.get(EDGAR_TICKERS_URL).mock(
        return_value=httpx.Response(200, json=_load_json("edgar", "company_tickers.json"))
    )
    no_insider_filings = {
        "filings": {
            "recent": {"form": ["10-K"], "accessionNumber": ["x"], "primaryDocument": ["x.htm"]}
        }
    }
    respx.get(url__regex=EDGAR_SUBMISSIONS_RE).mock(
        return_value=httpx.Response(200, json=no_insider_filings)
    )
    http = _make_http_client(tmp_path)
    try:
        resolver = EdgarResolver(http=http, contact="EmailMarketingTool research@example.invalid")
        candidates = await resolver.resolve(_ctx(company_name="Apple Inc", country_code="US"))
    finally:
        await http.aclose()

    assert candidates == []


# --------------------------------------------------------------------------
# Coverage map
# --------------------------------------------------------------------------


def test_coverage_map_is_data_driven() -> None:
    assert len(COVERAGE) >= 3
    gb = registries_for("GB")
    assert {c.resolver_name for c in gb} == {"companies_house", "opencorporates"}
    us = registries_for("US")
    assert {c.resolver_name for c in us} == {"edgar", "opencorporates"}
    au = registries_for("AU")
    assert {c.resolver_name for c in au} == {"opencorporates"}


def test_has_free_registry() -> None:
    assert has_free_registry("GB") is True
    assert has_free_registry("US") is True
    assert has_free_registry("AU") is False


# --------------------------------------------------------------------------
# Small helper functions, tested directly
# --------------------------------------------------------------------------


def test_extract_postcode_handles_missing_and_no_digit_snippets() -> None:
    assert _extract_postcode(None) is None
    assert _extract_postcode("") is None
    assert _extract_postcode("Somewhere, Countryside") is None  # no digits anywhere
    assert _extract_postcode("12 High Street, Northgate, London, EC1A 1BB") == "EC1A 1BB"


@respx.mock
async def test_officer_role_without_mapping_gets_low_confidence(tmp_path: Path) -> None:
    """An officer_role Companies House might add later that we haven't
    mapped yet is recorded at low confidence rather than silently dropped
    — visible in telemetry instead of vanishing.
    """
    respx.get(url__regex=CH_SEARCH_RE).mock(
        return_value=httpx.Response(
            200, json=_load_json("companies_house", "search_northgate.json")
        )
    )
    respx.get(url__regex=CH_OFFICERS_RE).mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "name": "OKONKWO, Ada",
                        "officer_role": "judicial-factor",
                        "appointed_on": "2020-01-01",
                    }
                ]
            },
        )
    )
    http = _make_http_client(tmp_path)
    try:
        resolver = CompaniesHouseResolver(http=http, api_key="testkey")
        candidates = await resolver.resolve(_ctx(company_name="Northgate Dental Care"))
    finally:
        await http.aclose()

    assert len(candidates) == 1
    assert candidates[0].confidence == pytest.approx(0.30)
    assert candidates[0].extra["role_class"] == RoleClass.OTHER.value


def test_role_class_from_position_keywords() -> None:
    assert _role_class_from_position("Managing Director") is RoleClass.OWNER
    assert _role_class_from_position("General Manager") is RoleClass.MANAGER
    assert _role_class_from_position("Company Secretary") is RoleClass.OTHER
    assert _role_class_from_position("Something Unrecognised") is RoleClass.OTHER


def test_postcode_from_missing_registered_address() -> None:
    assert _postcode_from({}) is None
    assert _postcode_from({"registered_address": {}}) is None
    assert _postcode_from({"registered_address": {"postal_code": "2000"}}) == "2000"
