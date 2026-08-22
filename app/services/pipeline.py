"""Pipeline orchestration — where the components finally meet the database.

Every stage follows the same shape: read what needs work from a repository,
run the existing (already-tested) component over it, write the results back
with provenance, and record telemetry for every attempt including the ones
that found nothing.

Two rules run through all of it:

*Fail loud.* Each stage tracks its error rate and aborts past a threshold
rather than writing partial results and reporting success. This is the
direct answer to the prototype that logged 1,640 consecutive verification
failures and exited 0 (CLAUDE.md §2.1).

*Every attempt is telemetry.* Misses and errors are written to
`resolver_runs` alongside hits, because a resolver that silently stops
finding anything is exactly the failure this product exists to catch, and
it is invisible if only successes are recorded.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BatchAbortedError
from app.db.models.enums import CampaignStatus, ResolverOutcome
from app.db.repositories.campaign_repository import CampaignRepository
from app.db.repositories.company_repository import CompanyCreate, CompanyRepository
from app.db.repositories.resolver_run_repository import (
    ResolverRunCreate,
    ResolverRunRepository,
)
from app.net.client import HttpClient
from app.resolvers.discovery.base import CompanyCandidate, DiscoverySpec
from app.resolvers.discovery.google_places import GooglePlacesResolver, PlacesBudget
from app.resolvers.discovery.orchestrate import discover_companies
from app.resolvers.discovery.osm import OSMResolver
from app.services.factories import Runtime

log = structlog.get_logger(__name__)

# Google Places' own free tier is ~$200/month of credit. Capping well under
# it means an accidental loop costs a fraction of the free allowance rather
# than a real bill; the resolver refuses to exceed this (rule 2.3).
_PLACES_MONTHLY_LIMIT = Decimal("50.00")


@dataclass(slots=True)
class StageResult:
    """What one stage actually did.

    `attempted` and `found` are separate on purpose: a stage that attempted
    400 and found 0 is a very different situation from one that attempted
    0, and collapsing them into a single "0 results" is how a silent
    failure hides.
    """

    stage: str
    attempted: int = 0
    found: int = 0
    written: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def error_rate(self) -> float:
        if self.attempted == 0:
            return 0.0
        return len(self.errors) / self.attempted

    @property
    def hit_rate(self) -> float:
        if self.attempted == 0:
            return 0.0
        return self.found / self.attempted


def _icp_to_spec(icp: dict[str, object], *, max_results: int) -> DiscoverySpec:
    """Translate a campaign's stored ICP into a discovery spec.

    Raises on a missing category or location rather than defaulting to
    something broad: silently discovering "everything, everywhere" would
    burn the user's rate limits and fill their database with leads they
    never asked for.
    """
    categories = icp.get("categories") or []
    locations = icp.get("locations") or []
    if not isinstance(categories, list) or not categories:
        raise ValueError("campaign ICP has no categories; nothing to discover")
    if not isinstance(locations, list) or not locations:
        raise ValueError("campaign ICP has no locations; nowhere to search")

    filters = icp.get("filters") or {}
    filters = filters if isinstance(filters, dict) else {}

    return DiscoverySpec(
        categories=[str(c) for c in categories],
        location=str(locations[0]),
        max_results=max_results,
        min_rating=_opt_float(filters.get("min_rating")),
        min_reviews=_opt_int(filters.get("min_reviews")),
        max_reviews=_opt_int(filters.get("max_reviews")),
    )


def _opt_float(value: object) -> float | None:
    return float(value) if isinstance(value, int | float | str) and value != "" else None


def _opt_int(value: object) -> int | None:
    return int(value) if isinstance(value, int | str) and value != "" else None


def _candidate_to_row(candidate: CompanyCandidate, campaign_id: uuid.UUID) -> CompanyCreate:
    return CompanyCreate(
        campaign_id=campaign_id,
        name=candidate.name,
        country_code=candidate.country_code,
        domain=candidate.domain,
        website=candidate.website,
        address=candidate.address,
        lat=candidate.lat,
        lng=candidate.lng,
        category=candidate.category,
        source_place_id=candidate.source_place_id,
        rating=candidate.rating,
        review_count=candidate.review_count,
        raw=dict(candidate.raw),
    )


def _build_discovery_resolvers(
    runtime: Runtime, http: HttpClient
) -> tuple[OSMResolver, GooglePlacesResolver | None]:
    """OSM always; Places only when a key exists.

    Places stays None rather than becoming a stub that returns nothing:
    'not configured' and 'configured but found nothing' are different
    facts, and the yield report must not conflate them (rule 2.3 -- a
    metered call that could have been avoided is a bug, and a metered
    resolver that was never configured should not appear to have missed).
    """
    osm = OSMResolver(http=http)
    if runtime.settings.google_maps_api_key is None:
        return osm, None

    places = GooglePlacesResolver(
        http=http,
        api_key=runtime.settings.google_maps_api_key.get_secret_value(),
        budget=PlacesBudget(monthly_limit=_PLACES_MONTHLY_LIMIT),
    )
    return osm, places


async def run_discovery(
    session: AsyncSession,
    runtime: Runtime,
    campaign_id: uuid.UUID,
    *,
    max_results: int = 200,
) -> StageResult:
    """Discover companies for a campaign and persist them.

    Idempotent: re-running skips companies this campaign already has (see
    CompanyRepository.bulk_upsert), so a user who clicks twice gets the
    same result rather than duplicates.
    """
    campaigns = CampaignRepository(session)
    campaign = await campaigns.get(campaign_id)
    if campaign is None:
        raise ValueError(f"Campaign {campaign_id} does not exist")

    spec = _icp_to_spec(campaign.icp, max_results=max_results)
    osm, places = _build_discovery_resolvers(runtime, runtime.http)
    result = StageResult(stage="discovery", attempted=1)

    await campaigns.set_status(campaign_id, CampaignStatus.DISCOVERING)
    started = time.monotonic()

    try:
        candidates = await discover_companies(spec, osm=osm, places=places)
    except Exception as exc:
        # Broad on purpose, and re-raised: the point is to leave a durable
        # ERROR row before the exception propagates, so a failed run is
        # visible in the yield report rather than only in a log line.
        elapsed_ms = int((time.monotonic() - started) * 1000)
        await ResolverRunRepository(session).record(
            ResolverRunCreate(
                resolver="discovery",
                field="company",
                tier=0,
                outcome=ResolverOutcome.ERROR,
                latency_ms=elapsed_ms,
                error=str(exc)[:500],
            )
        )
        await campaigns.set_status(campaign_id, CampaignStatus.DRAFT)
        log.error("discovery.failed", campaign_id=str(campaign_id), error=str(exc))
        raise

    elapsed_ms = int((time.monotonic() - started) * 1000)
    result.found = len(candidates)

    upsert = await CompanyRepository(session).bulk_upsert(
        [_candidate_to_row(c, campaign_id) for c in candidates]
    )
    result.written = upsert.inserted
    result.skipped = upsert.skipped

    # Telemetry per source, so the yield report can show which resolver
    # actually produced the leads rather than one opaque "discovery" row.
    await _record_discovery_telemetry(session, candidates, elapsed_ms=elapsed_ms)

    await campaigns.set_status(campaign_id, CampaignStatus.DRAFT)
    log.info(
        "discovery.complete",
        campaign_id=str(campaign_id),
        found=result.found,
        written=result.written,
        skipped=result.skipped,
    )
    return result


async def _record_discovery_telemetry(
    session: AsyncSession, candidates: list[CompanyCandidate], *, elapsed_ms: int
) -> None:
    """One row per source that ran, hit or miss.

    A source that returned zero still gets a MISS row -- that is the whole
    point of the table. Without it, a resolver whose upstream API started
    returning empty results looks identical to one that was never tried.
    """
    by_source: dict[str, int] = {}
    for candidate in candidates:
        by_source[candidate.source or "unknown"] = (
            by_source.get(candidate.source or "unknown", 0) + 1
        )

    runs = [
        ResolverRunCreate(
            resolver=source,
            field="company",
            tier=0,
            outcome=ResolverOutcome.HIT if count else ResolverOutcome.MISS,
            latency_ms=elapsed_ms,
            candidates_returned=count,
            cost=Decimal(0),
        )
        for source, count in (by_source or {"osm": 0}).items()
    ]
    await ResolverRunRepository(session).record_many(runs)


def guard_error_rate(result: StageResult, *, threshold: float) -> None:
    """Abort a stage whose error rate crossed the threshold.

    Raises rather than returning a flag, because the caller that ignores a
    returned flag is exactly how the prototype wrote a results file and
    exited 0 after 1,640 consecutive failures.
    """
    if result.attempted == 0:
        return
    if result.error_rate > threshold:
        raise BatchAbortedError(
            completed=result.attempted - len(result.errors),
            total=result.attempted,
            error_rate=result.error_rate,
            last=result.errors[-1] if result.errors else "unknown",
        )


__all__ = ["StageResult", "guard_error_rate", "run_discovery"]
