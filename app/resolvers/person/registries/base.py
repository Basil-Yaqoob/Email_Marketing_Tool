"""Shared matching and name-parsing logic for company registries.

Every registry resolver has to answer the same question first: is this
`RegistryCompany` actually the business the lead is about? A website says
"Northgate Dental"; the register says "NORTHGATE DENTAL CARE LIMITED".
Getting that match wrong attributes a stranger's name to a business — worse
than no name at all — so the bar to accept a match is deliberately high
(0.80, see doc/plans/session-07-registries.md) and postcode agreement is
what pushes a plausible-but-not-certain name match over the line.

Officer-name parsing lives here too because every registry hands back names
in a slightly different shape (Companies House: "SMITH, Jane Elizabeth";
OpenCorporates: usually "Jane Elizabeth Smith") and every downstream
consumer — email pattern generation in Session 09 — needs first/last split
out correctly regardless of source.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from app.resolvers.discovery.dedup import normalise_name

MATCH_THRESHOLD = 0.80
POSTCODE_BONUS = 0.10
# A name similarity below this isn't worth reporting as "matched on name"
# even as a component of a postcode-boosted score — it's just noise.
NAME_MATCH_FLOOR = 0.50

# Particles that stay lowercase inside a name but not at the very start of
# a name part (surname parsed alone still reads "Van der Berg", matching
# how it's written when it's the only thing on the line).
_LOWERCASE_PARTICLES = frozenset({"van", "der", "den", "de", "von", "af", "av", "du", "la"})
_WORD_BOUNDARY_RE = re.compile(r"([\'\-])")


@dataclass(frozen=True, slots=True)
class CompanyQuery:
    """What we already know about the lead's company, used to search a
    registry and to score whatever it returns.
    """

    name: str
    country_code: str
    postcode: str | None = None
    address: str | None = None


@dataclass(frozen=True, slots=True)
class RegistryCompany:
    """One candidate row a registry search returned, before matching."""

    company_number: str
    name: str
    entity_type: str
    postcode: str | None = None
    address: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RegistryMatch:
    company_number: str
    registered_name: str
    match_score: float
    matched_on: list[str]
    entity_type: str


def _normalise_postcode(postcode: str) -> str:
    return re.sub(r"\s+", "", postcode).upper()


def score_company_match(query: CompanyQuery, candidate: RegistryCompany) -> RegistryMatch:
    """Fuzzy name similarity, boosted by postcode agreement.

    The caller must reject anything below MATCH_THRESHOLD — this function
    only scores, it never decides. Name similarity alone can reach 1.0 (an
    exact match after normalisation), but two similarly-named businesses in
    different towns is exactly the failure mode CLAUDE.md 2.2 exists to
    prevent, so a borderline name match needs the postcode to agree before
    it's accepted.
    """
    name_sim = SequenceMatcher(
        None, normalise_name(query.name), normalise_name(candidate.name)
    ).ratio()

    matched_on: list[str] = []
    if name_sim >= NAME_MATCH_FLOOR:
        matched_on.append("name")

    score = name_sim
    if (
        query.postcode
        and candidate.postcode
        and _normalise_postcode(query.postcode) == _normalise_postcode(candidate.postcode)
    ):
        matched_on.append("postcode")
        score = min(1.0, score + POSTCODE_BONUS)

    return RegistryMatch(
        company_number=candidate.company_number,
        registered_name=candidate.name,
        match_score=score,
        matched_on=matched_on,
        entity_type=candidate.entity_type,
    )


@dataclass(frozen=True, slots=True)
class OfficerName:
    first: str
    middle: str | None
    last: str
    full: str  # natural reading order: "Jane Elizabeth Smith"


def _capitalise_word(word: str) -> str:
    """Capitalise a word, respecting internal apostrophes and hyphens —
    "o'brien" -> "O'Brien", "smith-jones" -> "Smith-Jones".
    """
    parts = _WORD_BOUNDARY_RE.split(word.lower())
    return "".join(p if p in ("'", "-") else p.capitalize() for p in parts)


def _title_case_name_part(part: str) -> str:
    words = part.strip().split()
    out = []
    for i, word in enumerate(words):
        if i > 0 and word.lower() in _LOWERCASE_PARTICLES:
            out.append(word.lower())
        else:
            out.append(_capitalise_word(word))
    return " ".join(out)


def parse_officer_name(raw: str) -> OfficerName:
    """Registries hand back names in two shapes:

      "SMITH, Jane Elizabeth"   (Companies House: surname first, comma-separated)
      "Jane Elizabeth Smith"    (OpenCorporates and most others: natural order)

    Both are parsed into first/middle/last. A single-word name (rare, but
    real for some sole traders) becomes the surname with no forename rather
    than guessing.
    """
    if "," in raw:
        surname_part, _, forenames_part = raw.partition(",")
    else:
        words = raw.strip().split()
        if len(words) < 2:
            surname_part, forenames_part = raw, ""
        else:
            forenames_part = " ".join(words[:-1])
            surname_part = words[-1]

    surname = _title_case_name_part(surname_part)
    forenames = [_title_case_name_part(w) for w in forenames_part.strip().split()]

    first = forenames[0] if forenames else ""
    middle = " ".join(forenames[1:]) if len(forenames) > 1 else None
    full = " ".join([*forenames, surname]).strip()

    return OfficerName(first=first, middle=middle, last=surname, full=full)
