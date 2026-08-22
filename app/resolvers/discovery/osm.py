"""OpenStreetMap discovery resolver — the free, unlimited first tier.

Geocodes the target location via Nominatim (1 req/sec hard limit — a
shared community resource, not just an API to be polite to), then queries
the Overpass API for businesses matching the requested categories.

OSM carries a `website` tag for a surprising share of businesses, which is
all the crawler (Session 06) needs to get started. That's why this runs
before Google Places rather than after, as the prototype did — since
March 2025 Google no longer gives a pooled free credit, so every
Places-first query was a wasted opportunity to get the same lead for free.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any
from urllib.parse import quote

from app.net.client import HttpClient
from app.resolvers.base import Tier
from app.resolvers.discovery.base import CompanyCandidate, DiscoverySpec
from app.resolvers.discovery.category_tags import load_category_tags, tags_for
from app.resolvers.discovery.util import ensure_scheme, normalise_domain

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_TIMEOUT_S = 60


class OSMResolver:
    name = "osm"
    tier = Tier.FREE
    cost_per_call = Decimal("0")
    jurisdictions: frozenset[str] | None = None  # worldwide

    def __init__(
        self,
        *,
        http: HttpClient,
        category_tags: Mapping[str, Sequence[Mapping[str, str]]] | None = None,
    ) -> None:
        self._http = http
        self._category_tags = category_tags or load_category_tags()

    async def discover(self, spec: DiscoverySpec) -> list[CompanyCandidate]:
        lat, lon, geocoded_country = await self._geocode(spec.location)
        query = self._build_query(spec, lat=lat, lon=lon)
        response = await self._http.get(f"{OVERPASS_URL}?data={quote(query)}", respect_robots=False)
        data = json.loads(response.text)
        elements = data.get("elements", [])

        candidates: list[CompanyCandidate] = []
        for element in elements:
            candidate = self._element_to_candidate(element, fallback_country=geocoded_country)
            if candidate is not None:
                candidates.append(candidate)
        return candidates

    async def _geocode(self, location: str) -> tuple[float, float, str | None]:
        response = await self._http.get(
            f"{NOMINATIM_URL}?q={quote(location)}&format=json&limit=1&addressdetails=1",
            respect_robots=False,
        )
        results = json.loads(response.text)
        if not results:
            raise ValueError(f"could not geocode location: {location!r}")

        first = results[0]
        lat = float(first["lat"])
        lon = float(first["lon"])
        country = first.get("address", {}).get("country_code")
        return lat, lon, country.upper() if country else None

    def _build_query(self, spec: DiscoverySpec, *, lat: float, lon: float) -> str:
        clauses: list[str] = []
        for category in spec.categories:
            # tags_for raises on an unmapped category rather than skipping
            # it. Skipping meant an unrecognised word contributed no
            # clauses, so a spec naming only unknown categories produced a
            # syntactically valid query with an empty body -- OSM returned
            # zero elements and the run looked successful (CLAUDE.md 2.1).
            for tag_set in tags_for(category, self._category_tags):
                for key, value in tag_set.items():
                    clauses.append(f'node["{key}"="{value}"](around:{spec.radius_m},{lat},{lon});')
        body = "\n  ".join(clauses)
        return f"[out:json][timeout:{OVERPASS_TIMEOUT_S}];\n(\n  {body}\n);\nout body;"

    def _element_to_candidate(
        self, element: Mapping[str, Any], *, fallback_country: str | None
    ) -> CompanyCandidate | None:
        tags = element.get("tags", {})
        name = tags.get("name")
        if not name:
            return None

        country_code = tags.get("addr:country") or fallback_country
        if not country_code:
            # Can't persist a company without a jurisdiction — the policy
            # engine (Session 15) has nothing to gate on.
            return None

        website = tags.get("website") or tags.get("contact:website")

        address_parts = [
            tags.get("addr:housenumber"),
            tags.get("addr:street"),
            tags.get("addr:city"),
            tags.get("addr:postcode"),
        ]
        address = " ".join(p for p in address_parts if p) or None

        return CompanyCandidate(
            name=name,
            country_code=country_code.upper(),
            domain=normalise_domain(website) if website else None,
            website=ensure_scheme(website) if website else None,
            address=address,
            phone=tags.get("phone") or tags.get("contact:phone"),
            lat=element.get("lat"),
            lng=element.get("lon"),
            source_place_id=f"osm:{element.get('type')}/{element.get('id')}",
            source=self.name,
            confidence=0.7,
            raw=dict(element),
        )
