"""Unit tests for company discovery: OSM, Google Places, dedup and the
waterfall-style orchestration. Network is always mocked with respx —
real saved fixtures under tests/fixtures/json/{osm,places}/ where the test
calls for one; tests 2, 8 and 9 are the load-bearing ones (either-tag
website extraction feeding country_code derivation, the budget guard, and
the cost guarantee that Places never fires when OSM already sufficed).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from app.core.errors import BudgetExceededError
from app.net.cache import ResponseCache
from app.net.client import HttpClient
from app.net.ratelimit import RateLimiter
from app.net.robots import RobotsChecker
from app.resolvers.base import Tier
from app.resolvers.discovery.base import CompanyCandidate, DiscoverySpec
from app.resolvers.discovery.dedup import deduplicate, normalise_name
from app.resolvers.discovery.google_places import GooglePlacesResolver, PlacesBudget
from app.resolvers.discovery.orchestrate import discover_companies
from app.resolvers.discovery.osm import OSMResolver

USER_AGENT = "EmailMarketingToolBot/1.0 (+https://example.invalid/bot; contact@example.invalid)"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "json"

NOMINATIM_PATTERN = r"https://nominatim\.openstreetmap\.org/search.*"
OVERPASS_PATTERN = r"https://overpass-api\.de/api/interpreter.*"
PLACES_URL = "https://places.googleapis.com/v1/places:searchText"


def _load_json(*parts: str) -> dict:
    return json.loads((FIXTURES / Path(*parts)).read_text(encoding="utf-8"))


def _load_text(*parts: str) -> str:
    return (FIXTURES / Path(*parts)).read_text(encoding="utf-8")


def _make_http_client(tmp_path: Path) -> HttpClient:
    cache = ResponseCache(tmp_path / "cache")
    limiter = RateLimiter(requests_per_second=1000.0, burst=1000, global_concurrency=1000)
    robots = RobotsChecker(user_agent=USER_AGENT)
    return HttpClient(cache=cache, limiter=limiter, robots=robots, user_agent=USER_AGENT)


def _spec(**overrides: object) -> DiscoverySpec:
    defaults: dict[str, object] = {"categories": ["dental clinic"], "location": "Austin TX"}
    defaults.update(overrides)
    return DiscoverySpec(**defaults)  # type: ignore[arg-type]


class _FakeDiscoveryResolver:
    """A discovery resolver double that records whether it was ever called
    — used where the point of the test is proving it *wasn't*.
    """

    name = "fake"
    tier = Tier.METERED
    cost_per_call = Decimal("0.05")
    jurisdictions: frozenset[str] | None = None

    def __init__(self, candidates: list[CompanyCandidate] | None = None) -> None:
        self.call_count = 0
        self._candidates = candidates or []

    async def discover(self, spec: DiscoverySpec) -> list[CompanyCandidate]:
        self.call_count += 1
        return self._candidates


# --------------------------------------------------------------------------
# OSM
# --------------------------------------------------------------------------


@respx.mock
async def test_osm_parses_overpass_response(tmp_path: Path) -> None:
    respx.get(url__regex=NOMINATIM_PATTERN).mock(
        return_value=httpx.Response(200, text=_load_text("osm", "nominatim_austin.json"))
    )
    respx.get(url__regex=OVERPASS_PATTERN).mock(
        return_value=httpx.Response(200, text=_load_text("osm", "dental_austin_overpass.json"))
    )
    http = _make_http_client(tmp_path)
    try:
        resolver = OSMResolver(http=http)
        candidates = await resolver.discover(_spec())
    finally:
        await http.aclose()

    names = {c.name for c in candidates}
    assert names == {"Northgate Dental", "Riverside Dental Care"}


@respx.mock
async def test_osm_extracts_website_from_either_tag(tmp_path: Path) -> None:
    respx.get(url__regex=NOMINATIM_PATTERN).mock(
        return_value=httpx.Response(200, text=_load_text("osm", "nominatim_austin.json"))
    )
    respx.get(url__regex=OVERPASS_PATTERN).mock(
        return_value=httpx.Response(200, text=_load_text("osm", "dental_austin_overpass.json"))
    )
    http = _make_http_client(tmp_path)
    try:
        resolver = OSMResolver(http=http)
        candidates = await resolver.discover(_spec())
    finally:
        await http.aclose()

    by_name = {c.name: c for c in candidates}
    assert by_name["Northgate Dental"].domain == "northgatedental.com"  # "website" tag
    assert by_name["Riverside Dental Care"].domain == "riversidedentalcare.com"  # "contact:website"


@respx.mock
async def test_osm_sets_country_code(tmp_path: Path) -> None:
    respx.get(url__regex=NOMINATIM_PATTERN).mock(
        return_value=httpx.Response(200, text=_load_text("osm", "nominatim_austin.json"))
    )
    respx.get(url__regex=OVERPASS_PATTERN).mock(
        return_value=httpx.Response(200, text=_load_text("osm", "dental_austin_overpass.json"))
    )
    http = _make_http_client(tmp_path)
    try:
        resolver = OSMResolver(http=http)
        candidates = await resolver.discover(_spec())
    finally:
        await http.aclose()

    # Northgate has addr:country="US" directly; Riverside has none and must
    # fall back to the geocode's country_code.
    assert all(c.country_code == "US" for c in candidates)


@respx.mock
async def test_osm_skips_elements_without_a_name(tmp_path: Path) -> None:
    respx.get(url__regex=NOMINATIM_PATTERN).mock(
        return_value=httpx.Response(200, text=_load_text("osm", "nominatim_austin.json"))
    )
    respx.get(url__regex=OVERPASS_PATTERN).mock(
        return_value=httpx.Response(200, text=_load_text("osm", "dental_austin_overpass.json"))
    )
    http = _make_http_client(tmp_path)
    try:
        resolver = OSMResolver(http=http)
        candidates = await resolver.discover(_spec())
    finally:
        await http.aclose()

    # Fixture has 3 elements; one has no "name" tag at all and must be
    # dropped, not persisted as junk.
    raw = _load_json("osm", "dental_austin_overpass.json")
    assert len(raw["elements"]) == 3
    assert len(candidates) == 2


@respx.mock
async def test_osm_raises_on_unresolvable_location(tmp_path: Path) -> None:
    respx.get(url__regex=NOMINATIM_PATTERN).mock(return_value=httpx.Response(200, text="[]"))
    http = _make_http_client(tmp_path)
    try:
        resolver = OSMResolver(http=http)
        with pytest.raises(ValueError, match="could not geocode"):
            await resolver.discover(_spec(location="Nowhere At All"))
    finally:
        await http.aclose()


@respx.mock
async def test_osm_drops_element_with_no_country_code_anywhere(tmp_path: Path) -> None:
    # Geocode result deliberately carries no country_code, and the element
    # itself has no addr:country tag either -- nothing to fall back to.
    respx.get(url__regex=NOMINATIM_PATTERN).mock(
        return_value=httpx.Response(
            200, text=json.dumps([{"lat": "30.0", "lon": "-97.0", "address": {}}])
        )
    )
    respx.get(url__regex=OVERPASS_PATTERN).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "elements": [
                        {
                            "type": "node",
                            "id": 1,
                            "lat": 30.0,
                            "lon": -97.0,
                            "tags": {"amenity": "dentist", "name": "No Country Dental"},
                        }
                    ]
                }
            ),
        )
    )
    http = _make_http_client(tmp_path)
    try:
        resolver = OSMResolver(http=http)
        candidates = await resolver.discover(_spec())
    finally:
        await http.aclose()

    assert candidates == []


# --------------------------------------------------------------------------
# Google Places
# --------------------------------------------------------------------------


@respx.mock
async def test_places_parses_search_text_response(tmp_path: Path) -> None:
    respx.post(PLACES_URL).mock(
        return_value=httpx.Response(200, text=_load_text("places", "dental_austin_page2.json"))
    )
    http = _make_http_client(tmp_path)
    try:
        resolver = GooglePlacesResolver(
            http=http, api_key="test-key", budget=PlacesBudget(monthly_limit=Decimal("100"))
        )
        candidates = await resolver.discover(_spec())
    finally:
        await http.aclose()

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.name == "Lakeside Family Dental"
    assert candidate.country_code == "US"
    assert candidate.rating == 4.2
    assert candidate.review_count == 54
    assert candidate.domain == "lakesidedental.example.com"


@respx.mock
async def test_places_field_mask_requests_only_persisted_fields(tmp_path: Path) -> None:
    route = respx.post(PLACES_URL).mock(
        return_value=httpx.Response(200, text=_load_text("places", "dental_austin_page2.json"))
    )
    http = _make_http_client(tmp_path)
    try:
        resolver = GooglePlacesResolver(
            http=http, api_key="test-key", budget=PlacesBudget(monthly_limit=Decimal("100"))
        )
        await resolver.discover(_spec())
    finally:
        await http.aclose()

    sent_mask = route.calls.last.request.headers["X-Goog-FieldMask"]
    assert "places.displayName" in sent_mask
    assert "places.rating" in sent_mask
    # The expensive field that jumps to a pricier SKU must never be requested.
    assert "places.reviews" not in sent_mask


@respx.mock
async def test_places_paginates(tmp_path: Path) -> None:
    route = respx.post(PLACES_URL)
    route.side_effect = [
        httpx.Response(200, text=_load_text("places", "dental_austin_page1.json")),
        httpx.Response(200, text=_load_text("places", "dental_austin_page2.json")),
    ]
    http = _make_http_client(tmp_path)
    try:
        resolver = GooglePlacesResolver(
            http=http, api_key="test-key", budget=PlacesBudget(monthly_limit=Decimal("100"))
        )
        candidates = await resolver.discover(_spec())
    finally:
        await http.aclose()

    assert route.call_count == 2
    names = {c.name for c in candidates}
    assert names == {"Sunshine Dental", "Lakeside Family Dental"}


@respx.mock
async def test_places_skips_result_with_no_display_name(tmp_path: Path) -> None:
    respx.post(PLACES_URL).mock(
        return_value=httpx.Response(200, text=json.dumps({"places": [{"id": "no-name-place"}]}))
    )
    http = _make_http_client(tmp_path)
    try:
        resolver = GooglePlacesResolver(
            http=http, api_key="test-key", budget=PlacesBudget(monthly_limit=Decimal("100"))
        )
        candidates = await resolver.discover(_spec())
    finally:
        await http.aclose()

    assert candidates == []


@respx.mock
async def test_places_skips_result_with_no_country_component(tmp_path: Path) -> None:
    respx.post(PLACES_URL).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "places": [
                        {
                            "id": "no-country-place",
                            "displayName": {"text": "Mystery Clinic"},
                            "addressComponents": [],
                        }
                    ]
                }
            ),
        )
    )
    http = _make_http_client(tmp_path)
    try:
        resolver = GooglePlacesResolver(
            http=http, api_key="test-key", budget=PlacesBudget(monthly_limit=Decimal("100"))
        )
        candidates = await resolver.discover(_spec())
    finally:
        await http.aclose()

    assert candidates == []


def test_places_budget_tracks_spend_across_calls() -> None:
    budget = PlacesBudget(monthly_limit=Decimal("1.00"))
    assert budget.spent_this_month() == Decimal("0")

    budget.check_and_record(Decimal("0.30"))
    budget.check_and_record(Decimal("0.20"))

    assert budget.spent_this_month() == Decimal("0.50")


@respx.mock
async def test_places_budget_raises_when_exceeded(tmp_path: Path) -> None:
    route = respx.post(PLACES_URL).mock(
        return_value=httpx.Response(200, text=_load_text("places", "dental_austin_page2.json"))
    )
    http = _make_http_client(tmp_path)
    try:
        resolver = GooglePlacesResolver(
            http=http,
            api_key="test-key",
            budget=PlacesBudget(monthly_limit=Decimal("0.01")),  # less than one request's cost
        )
        with pytest.raises(BudgetExceededError):
            await resolver.discover(_spec())
    finally:
        await http.aclose()

    assert route.call_count == 0, "budget check must happen before the request, not after"


async def test_places_not_called_when_osm_satisfied_the_spec() -> None:
    """The waterfall applied to discovery: Places is metered, OSM is free
    — Places must not run once OSM's yield already meets max_results.
    """
    osm_candidates = [
        CompanyCandidate(name="Northgate Dental", country_code="US", source="osm"),
        CompanyCandidate(name="Riverside Dental Care", country_code="US", source="osm"),
    ]
    osm = _FakeDiscoveryResolver(osm_candidates)
    places = _FakeDiscoveryResolver([])

    result = await discover_companies(_spec(max_results=2), osm=osm, places=places)

    assert places.call_count == 0
    assert len(result) == 2


async def test_places_called_when_osm_leaves_a_gap() -> None:
    """The positive case: Places does run when OSM's yield falls short."""
    osm = _FakeDiscoveryResolver(
        [CompanyCandidate(name="Northgate Dental", country_code="US", source="osm")]
    )
    places = _FakeDiscoveryResolver(
        [CompanyCandidate(name="Sunshine Dental", country_code="US", source="google_places")]
    )

    result = await discover_companies(_spec(max_results=10), osm=osm, places=places)

    assert places.call_count == 1
    assert len(result) == 2


# --------------------------------------------------------------------------
# Dedup
# --------------------------------------------------------------------------


def test_dedup_merges_on_exact_domain() -> None:
    a = CompanyCandidate(
        name="Northgate Dental", country_code="US", domain="northgatedental.com", source="osm"
    )
    b = CompanyCandidate(
        name="Northgate Dental Care Ltd",
        country_code="US",
        domain="northgatedental.com",
        source="google_places",
    )

    merged = deduplicate([a, b])

    assert len(merged) == 1


def test_dedup_merges_on_name_and_proximity() -> None:
    # ~0.00045 degrees of latitude is roughly 50m.
    a = CompanyCandidate(
        name="Northgate Dental", country_code="US", lat=30.0, lng=-97.0, source="osm"
    )
    b = CompanyCandidate(
        name="Northgate Dental Ltd",
        country_code="US",
        lat=30.00045,
        lng=-97.0,
        source="google_places",
    )

    merged = deduplicate([a, b])

    assert len(merged) == 1


def test_dedup_keeps_distant_same_name_separate() -> None:
    # ~0.045 degrees of latitude is roughly 5km.
    a = CompanyCandidate(name="Northgate Dental", country_code="US", lat=30.0, lng=-97.0)
    b = CompanyCandidate(name="Northgate Dental", country_code="US", lat=30.045, lng=-97.0)

    merged = deduplicate([a, b])

    assert len(merged) == 2


def test_dedup_ignores_candidates_with_no_coordinates() -> None:
    """A candidate with no lat/lng can still match on domain, but must
    never crash or false-match during the proximity pass.
    """
    no_coords = CompanyCandidate(name="Mystery Dental", country_code="US")
    nearby_different_name = CompanyCandidate(
        name="Totally Different Clinic", country_code="US", lat=30.0, lng=-97.0
    )
    candidate = CompanyCandidate(name="Mystery Dental", country_code="US", lat=30.0, lng=-97.0)

    merged = deduplicate([no_coords, nearby_different_name, candidate])

    # no_coords has no domain and no coordinates, so it can't match
    # anything on either signal -- all three stay distinct.
    assert len(merged) == 3


def test_dedup_union_prefers_higher_confidence_field() -> None:
    higher = CompanyCandidate(
        name="Acme Dental",
        country_code="US",
        domain="acme.com",
        phone=None,
        rating=None,
        source="osm",
        confidence=0.9,
    )
    lower = CompanyCandidate(
        name="Acme Dental Inc",
        country_code="US",
        domain="acme.com",
        phone="555-1234",
        rating=4.5,
        source="google_places",
        confidence=0.6,
    )

    merged = deduplicate([higher, lower])

    assert len(merged) == 1
    result = merged[0]
    assert result.name == "Acme Dental"  # from the higher-confidence source
    assert result.phone == "555-1234"  # filled in from the lower-confidence source
    assert result.rating == 4.5


@pytest.mark.parametrize(
    ("raw_name", "expected"),
    [
        ("Acme Dental Ltd", "acme dental"),
        ("Acme Dental LLC", "acme dental"),
        ("Acme Dental GmbH", "acme dental"),
        ("Acme Dental Inc", "acme dental"),
        ("Acme Dental PLLC", "acme dental"),
    ],
)
def test_name_normalisation_strips_legal_suffixes(raw_name: str, expected: str) -> None:
    assert normalise_name(raw_name) == expected


# --------------------------------------------------------------------------
# Orchestration: filters
# --------------------------------------------------------------------------


async def test_rating_and_review_filters_applied() -> None:
    candidates = [
        CompanyCandidate(name="Qualifies", country_code="US", rating=4.5, review_count=50),
        CompanyCandidate(name="Too few reviews", country_code="US", rating=4.8, review_count=2),
        CompanyCandidate(name="Rating too low", country_code="US", rating=2.0, review_count=80),
        CompanyCandidate(name="Too many reviews", country_code="US", rating=4.5, review_count=5000),
        CompanyCandidate(name="No rating data", country_code="US", rating=None, review_count=None),
    ]
    osm = _FakeDiscoveryResolver(candidates)
    spec = _spec(min_rating=4.0, min_reviews=10, max_reviews=1000)

    result = await discover_companies(spec, osm=osm, places=None)

    assert {c.name for c in result} == {"Qualifies"}


# --------------------------------------------------------------------------
# Yield floor canary
# --------------------------------------------------------------------------


def _synthetic_overpass_response(query_index: int) -> dict:
    """A synthetic Overpass-shaped response for the yield-floor canary.

    Not a live capture -- fetching 20 real Overpass responses isn't
    possible in this environment. Structurally varied (some elements use
    "website", some "contact:website", some neither; some carry
    addr:country, some rely on the geocode fallback; a few are nameless
    junk) so the test exercises the same parsing paths a real response
    would, without pretending to be one.
    """
    elements = []
    for i in range(15):
        tags: dict[str, str] = {"amenity": "dentist"}
        if i % 5 != 4:  # 12 of 15 have a name; the rest are junk to skip
            tags["name"] = f"Clinic {query_index}-{i}"
        if i % 3 == 0:
            tags["website"] = f"clinic{query_index}-{i}.example.com"
        elif i % 3 == 1:
            tags["contact:website"] = f"clinic{query_index}-{i}.example.com"
        if i % 2 == 0:
            tags["addr:country"] = "US"
        elements.append(
            {
                "type": "node",
                "id": query_index * 1000 + i,
                "lat": 30.0 + i * 0.001,
                "lon": -97.0 - i * 0.001,
                "tags": tags,
            }
        )
    return {"version": 0.6, "generator": "synthetic", "elements": elements}


async def test_discovery_yield_floor(tmp_path: Path) -> None:
    """20 synthetic queries must each yield >= 10 companies -- a canary
    against a regression that silently collapses the parser's yield, the
    exact failure mode this project exists to catch (CLAUDE.md rule 2.1).
    """
    nominatim_body = _load_text("osm", "nominatim_austin.json")

    for query_index in range(20):
        with respx.mock:
            respx.get(url__regex=NOMINATIM_PATTERN).mock(
                return_value=httpx.Response(200, text=nominatim_body)
            )
            respx.get(url__regex=OVERPASS_PATTERN).mock(
                return_value=httpx.Response(
                    200, text=json.dumps(_synthetic_overpass_response(query_index))
                )
            )
            http = _make_http_client(tmp_path)
            try:
                resolver = OSMResolver(http=http)
                candidates = await resolver.discover(_spec(location=f"City {query_index}"))
            finally:
                await http.aclose()

        assert len(candidates) >= 10, (
            f"query {query_index} yielded only {len(candidates)} companies, below the floor of 10"
        )
