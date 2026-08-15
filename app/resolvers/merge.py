"""Confidence merging.

If two *independent* sources both say the CEO is "Jane Smith", that is much
stronger evidence than either alone:

    combined = 1 - Π(1 - cᵢ)

Two sources at 0.6 -> 1 - (0.4 x 0.4) = 0.84. Capped at 0.99 -- never claim
certainty from finite evidence.

Independence is the subtlety: two SERP queries hitting the same search
index are not independent, and stacking them would manufacture false
confidence. Candidates sharing an independence_key are collapsed to their
single best member before combining.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from app.resolvers.base import Candidate

MAX_CONFIDENCE = 0.99


def _default_normalise(value: str) -> str:
    return value.strip().lower()


def merge_candidates(
    candidates: Sequence[Candidate],
    *,
    normalise: Callable[[str], str] = _default_normalise,
) -> list[Candidate]:
    """Group by normalised value, collapse each independence_key group to
    its strongest member, combine the remaining independent confidences,
    sort descending, cap at MAX_CONFIDENCE.

    Normalisation is field-specific and matters more than it looks —
    "Dr. Jane Smith", "Jane Smith" and "jane smith" should usually be one
    value. Callers pass the normaliser appropriate to the field being
    merged rather than this module guessing.
    """
    by_value: dict[str, list[Candidate]] = {}
    for c in candidates:
        by_value.setdefault(normalise(c.value), []).append(c)

    merged = [_merge_group(group) for group in by_value.values()]
    merged.sort(key=lambda c: c.confidence, reverse=True)
    return merged


def _merge_group(group: list[Candidate]) -> Candidate:
    """Merge every candidate agreeing on one normalised value."""
    independent = _collapse_correlated(group)

    if len(independent) == 1:
        return independent[0]

    product_of_misses = 1.0
    for c in independent:
        product_of_misses *= 1.0 - c.confidence
    combined_confidence = min(1.0 - product_of_misses, MAX_CONFIDENCE)

    best = max(independent, key=lambda c: c.confidence)
    return Candidate(
        value=best.value,
        confidence=combined_confidence,
        source=best.source,
        source_url=best.source_url,
        retrieved_at=best.retrieved_at,
        independence_key=None,
        extra={"merged_from": [c.source for c in independent]},
    )


def _collapse_correlated(group: list[Candidate]) -> list[Candidate]:
    """Candidates with no independence_key are each their own independent
    source. Candidates sharing a key are correlated — keep only the
    strongest one from each shared-key group.
    """
    by_key: dict[str, list[Candidate]] = {}
    independent: list[Candidate] = []
    for c in group:
        if c.independence_key is None:
            independent.append(c)
        else:
            by_key.setdefault(c.independence_key, []).append(c)

    for correlated_group in by_key.values():
        independent.append(max(correlated_group, key=lambda c: c.confidence))

    return independent
