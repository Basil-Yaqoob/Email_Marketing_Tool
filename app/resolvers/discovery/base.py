"""Shared types for company discovery.

Discovery is not quite a per-lead Resolver (app/resolvers/base.py) — it
*produces* leads rather than enriching one that already exists. It gets its
own protocol, but keeps the same tier and cost semantics so the waterfall
pattern (Session 03) still applies one level up: free sources run first,
metered sources fill gaps only.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from app.resolvers.base import Tier


@dataclass(frozen=True, slots=True)
class DiscoverySpec:
    categories: Sequence[str]  # ["dental clinic", "med spa"]
    location: str  # "Austin TX"
    radius_m: int = 25_000
    max_results: int = 500
    min_rating: float | None = None
    min_reviews: int | None = None
    max_reviews: int | None = None


@dataclass(frozen=True, slots=True)
class CompanyCandidate:
    name: str
    # ISO-3166-1 alpha-2. Required, not optional: the policy engine
    # (Session 15) depends on it, same as the companies table itself
    # (Session 02) — a candidate that can't determine this is dropped by
    # the resolver that would have produced it, not persisted with a gap.
    country_code: str
    domain: str | None = None
    website: str | None = None
    address: str | None = None
    phone: str | None = None
    lat: float | None = None
    lng: float | None = None
    category: str | None = None
    source_place_id: str | None = None
    rating: float | None = None
    review_count: int | None = None
    source: str = ""  # resolver name, e.g. "osm", "google_places"
    confidence: float = 1.0
    # Full raw payload from the source, kept for re-parsing without a
    # re-fetch — same reasoning as companies.raw (Session 02).
    raw: Mapping[str, Any] = field(default_factory=dict)


class DiscoveryResolver(Protocol):
    name: str
    tier: Tier
    cost_per_call: Decimal

    async def discover(self, spec: DiscoverySpec) -> list[CompanyCandidate]: ...
