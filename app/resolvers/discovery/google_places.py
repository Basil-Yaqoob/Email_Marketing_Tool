"""Google Places (New) discovery resolver — metered, gap-filling only.

Google retired the pooled $200/month credit in March 2025 and moved to
per-SKU free tiers (roughly 10k Essentials / 5k Pro / 1k Enterprise
events/month, each consuming its own allowance). At scale that's a real
bill, which is exactly why this resolver only runs when OSM leaves gaps
(app/resolvers/discovery/orchestrate.py) and every call is checked against
a PlacesBudget first — a metered call that could have been avoided is a
bug (CLAUDE.md rule 2.3).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from app.core.errors import BudgetExceededError
from app.net.client import HttpClient
from app.resolvers.base import Tier
from app.resolvers.discovery.base import CompanyCandidate, DiscoverySpec
from app.resolvers.discovery.util import normalise_domain

SEARCH_TEXT_URL = "https://places.googleapis.com/v1/places:searchText"

# Only the fields we actually persist. Requesting more (e.g.
# "places.reviews") jumps the request to a pricier SKU — check the Places
# pricing page before adding a field here.
FIELD_MASK = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.addressComponents",
        "places.location",
        "places.websiteUri",
        "places.internationalPhoneNumber",
        "places.rating",
        "places.userRatingCount",
        "nextPageToken",
    ]
)

# Text Search (Pro SKU) published per-request price, used for the budget
# guard's projection. Update this if Google's pricing changes.
COST_PER_REQUEST = Decimal("0.032")

DEFAULT_MAX_PAGES = 3


class PlacesBudget:
    """Refuses to exceed a configured monthly request budget.

    Raises BudgetExceededError rather than silently continuing — a
    surprise bill is exactly the metered-dependency failure this project
    exists to avoid (CLAUDE.md rule 2.3). Never a warning.
    """

    def __init__(
        self,
        *,
        monthly_limit: Decimal,
        clock: Callable[[], date] | None = None,
    ) -> None:
        self._monthly_limit = monthly_limit
        self._clock = clock or (lambda: datetime.now(UTC).date())
        self._spent: dict[str, Decimal] = {}  # "YYYY-MM" -> spent so far

    def _month_key(self) -> str:
        today = self._clock()
        return f"{today.year:04d}-{today.month:02d}"

    def check_and_record(self, cost: Decimal) -> None:
        key = self._month_key()
        spent_so_far = self._spent.get(key, Decimal("0"))
        projected = spent_so_far + cost
        if projected > self._monthly_limit:
            raise BudgetExceededError(
                f"Places budget exceeded for {key}: this call would bring spend to "
                f"${projected}, over the ${self._monthly_limit} monthly limit."
            )
        self._spent[key] = projected

    def spent_this_month(self) -> Decimal:
        return self._spent.get(self._month_key(), Decimal("0"))


class GooglePlacesResolver:
    name = "google_places"
    tier = Tier.METERED
    cost_per_call = COST_PER_REQUEST
    jurisdictions: frozenset[str] | None = None  # worldwide

    def __init__(
        self,
        *,
        http: HttpClient,
        api_key: str,
        budget: PlacesBudget,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> None:
        self._http = http
        self._api_key = api_key
        self._budget = budget
        self._max_pages = max_pages

    async def discover(self, spec: DiscoverySpec) -> list[CompanyCandidate]:
        candidates: list[CompanyCandidate] = []
        for category in spec.categories:
            candidates.extend(await self._search_text(category, spec))
        return candidates

    async def _search_text(self, category: str, spec: DiscoverySpec) -> list[CompanyCandidate]:
        results: list[CompanyCandidate] = []
        page_token: str | None = None

        for _ in range(self._max_pages):
            self._budget.check_and_record(self.cost_per_call)

            body: dict[str, Any] = {"textQuery": f"{category} in {spec.location}"}
            if page_token:
                body["pageToken"] = page_token

            response = await self._http.post(
                SEARCH_TEXT_URL,
                json=body,
                headers={
                    "X-Goog-Api-Key": self._api_key,
                    "X-Goog-FieldMask": FIELD_MASK,
                    "Content-Type": "application/json",
                },
            )
            data = json.loads(response.text)

            for place in data.get("places", []):
                candidate = self._place_to_candidate(place, category)
                if candidate is not None:
                    results.append(candidate)

            page_token = data.get("nextPageToken")
            if not page_token:
                break

        return results

    def _place_to_candidate(
        self, place: Mapping[str, Any], category: str
    ) -> CompanyCandidate | None:
        name = place.get("displayName", {}).get("text")
        if not name:
            return None

        country_code = None
        for component in place.get("addressComponents", []):
            if "country" in component.get("types", []):
                country_code = component.get("shortText")
                break
        if not country_code:
            return None

        location = place.get("location", {})
        website = place.get("websiteUri")

        return CompanyCandidate(
            name=name,
            country_code=country_code.upper(),
            domain=normalise_domain(website) if website else None,
            website=website,
            address=place.get("formattedAddress"),
            phone=place.get("internationalPhoneNumber"),
            lat=location.get("latitude"),
            lng=location.get("longitude"),
            category=category,
            source_place_id=place.get("id"),
            rating=place.get("rating"),
            review_count=place.get("userRatingCount"),
            source=self.name,
            confidence=0.9,
            raw=dict(place),
        )
