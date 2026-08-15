"""Deduplication across discovery sources.

The same business often appears in both OSM and Google Places with
slightly different names ("Northgate Dental" vs "Northgate Dental Care
Ltd."). Merge on domain first — exact and reliable — then on normalised
name plus geographic proximity. The merged record keeps the union of
fields, preferring the higher-confidence source per field, and keeps both
sources' raw payloads.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence

from app.resolvers.discovery.base import CompanyCandidate

PROXIMITY_METERS = 200.0
_EARTH_RADIUS_M = 6_371_000.0

_LEGAL_SUFFIXES = frozenset(
    {"ltd", "limited", "llc", "gmbh", "inc", "pllc", "pc", "co", "corp", "plc", "llp"}
)
_PUNCT_RE = re.compile(r"[^\w\s]")
_WHITESPACE_RE = re.compile(r"\s+")


def normalise_name(name: str) -> str:
    """Lowercase, strip legal suffixes, strip punctuation, collapse whitespace."""
    text = _PUNCT_RE.sub(" ", name.lower())
    words = [w for w in text.split() if w not in _LEGAL_SUFFIXES]
    return _WHITESPACE_RE.sub(" ", " ".join(words)).strip()


def _distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance via the haversine formula."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


def deduplicate(candidates: Sequence[CompanyCandidate]) -> list[CompanyCandidate]:
    merged: list[CompanyCandidate] = []
    for candidate in candidates:
        match_index = _find_match(merged, candidate)
        if match_index is None:
            merged.append(candidate)
        else:
            merged[match_index] = _merge(merged[match_index], candidate)
    return merged


def _find_match(existing: Sequence[CompanyCandidate], candidate: CompanyCandidate) -> int | None:
    # 1. Exact domain match — highest signal, checked first regardless of
    #    distance (a chain's HQ-listed coordinates can be miles from a
    #    branch, but the domain is unambiguous).
    if candidate.domain:
        for i, other in enumerate(existing):
            if other.domain and other.domain == candidate.domain:
                return i

    # 2. Normalised name + within PROXIMITY_METERS.
    if candidate.lat is not None and candidate.lng is not None:
        candidate_name = normalise_name(candidate.name)
        for i, other in enumerate(existing):
            if other.lat is None or other.lng is None:
                continue
            if normalise_name(other.name) != candidate_name:
                continue
            if _distance_m(candidate.lat, candidate.lng, other.lat, other.lng) <= PROXIMITY_METERS:
                return i

    return None


def _merge(a: CompanyCandidate, b: CompanyCandidate) -> CompanyCandidate:
    """Union of fields, preferring the higher-confidence source per field."""
    primary, secondary = (a, b) if a.confidence >= b.confidence else (b, a)

    raw = {
        f"source:{a.source or 'unknown'}": a.raw,
        f"source:{b.source or 'unknown'}": b.raw,
    }

    return CompanyCandidate(
        name=primary.name,
        country_code=primary.country_code,
        domain=primary.domain or secondary.domain,
        website=primary.website or secondary.website,
        address=primary.address or secondary.address,
        phone=primary.phone or secondary.phone,
        lat=primary.lat if primary.lat is not None else secondary.lat,
        lng=primary.lng if primary.lng is not None else secondary.lng,
        category=primary.category or secondary.category,
        source_place_id=primary.source_place_id or secondary.source_place_id,
        rating=primary.rating if primary.rating is not None else secondary.rating,
        review_count=(
            primary.review_count if primary.review_count is not None else secondary.review_count
        ),
        source=f"{a.source}+{b.source}" if a.source != b.source else a.source,
        confidence=max(a.confidence, b.confidence),
        raw=raw,
    )
