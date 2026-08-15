"""Company discovery orchestration — the waterfall pattern applied one
level up from app/resolvers/executor.py.

OSM runs first: free and unlimited. Google Places only runs to fill a gap
— when OSM's yield falls short of the campaign's requested count — because
a metered call that could have been avoided is a bug (CLAUDE.md rule 2.3).

Per-business "this OSM listing has no website, go look it up on Places" gap
filling is deliberately out of scope here: Places' text search finds
businesses, not specific missing fields for a business already found, and
that refinement belongs with the crawler (Session 06) or a dedicated
enrichment pass, not discovery. This orchestrator only escalates on total
count.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.resolvers.discovery.base import CompanyCandidate, DiscoveryResolver, DiscoverySpec
from app.resolvers.discovery.dedup import deduplicate


async def discover_companies(
    spec: DiscoverySpec,
    *,
    osm: DiscoveryResolver,
    places: DiscoveryResolver | None = None,
) -> list[CompanyCandidate]:
    osm_candidates = await osm.discover(spec)

    if places is not None and len(osm_candidates) < spec.max_results:
        places_candidates = await places.discover(spec)
        combined: list[CompanyCandidate] = [*osm_candidates, *places_candidates]
    else:
        combined = list(osm_candidates)

    filtered = _apply_filters(combined, spec)
    return deduplicate(filtered)


def _apply_filters(
    candidates: Sequence[CompanyCandidate], spec: DiscoverySpec
) -> list[CompanyCandidate]:
    """The prototype's qualification rules: established enough to have a
    marketing budget, small enough to still care about a cold email.
    """
    result: list[CompanyCandidate] = []
    for candidate in candidates:
        if spec.min_rating is not None and (
            candidate.rating is None or candidate.rating < spec.min_rating
        ):
            continue
        if spec.min_reviews is not None and (
            candidate.review_count is None or candidate.review_count < spec.min_reviews
        ):
            continue
        if (
            spec.max_reviews is not None
            and candidate.review_count is not None
            and candidate.review_count > spec.max_reviews
        ):
            continue
        result.append(candidate)
    return result
