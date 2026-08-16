"""Email pattern generation — first name + last name + domain -> ranked
candidate addresses.

The prototype generated nine blind guesses per lead and asked a paid
verifier to sort them out. When that vendor's credits ran dry the whole
stage collapsed, and `MAX_GUESSES_PER_CLINIC` was left at 0 — switching
pattern guessing off entirely and stranding 880 unchecked guesses.

Two things fix that, and both live here:

1. **Rank by real-world frequency.** If you must guess blind, trying
   `{first}.{last}` (~35% of companies) before `{last}.{first}` (~1%)
   finds most hits in the first few checks rather than the last few.
2. **Stop guessing once the domain is known.** `generate(known=...)`
   returns exactly ONE address. That is the ~9x reduction in metered
   verification cost that Session 10 depends on, and the reason
   domain_patterns (Session 02) is global rather than per-campaign.

Nothing generated here is ever treated as a real address. The highest
confidence any of it reaches is 0.80, deliberately below the 0.85 send
threshold — a generated address is a hypothesis until Session 10
verifies it.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

# Real-world corporate email format frequency. Shares are approximate and
# don't sum to 1 -- the long tail of rare formats isn't worth guessing.
PRIOR_FIRST_DOT_LAST = 0.35
CONF_KNOWN_CONFIRMED = 0.80  # known pattern, confirmed_count >= 2
CONF_KNOWN_SINGLE = 0.65  # known pattern, confirmed_count == 1
CONF_BLIND_TOP = 0.30  # unknown domain, top-ranked guess
CONF_BLIND_REST = 0.15  # unknown domain, everything below the top

# Cap on blind guesses. The prototype's nine is the number this session's
# economics are quoted against; ten keeps the top-frequency tail without
# turning a miss into a verification bill.
MAX_BLIND_GUESSES = 10

# Minimum plausible local part. Guards the mononym edge case, where
# templates needing a surname collapse to a single initial ("c@acme.com").
MIN_LOCAL_PART_LENGTH = 2


@dataclass(frozen=True, slots=True)
class Pattern:
    template: str
    prior: float
    # None = applies everywhere. Otherwise only generated for leads whose
    # industry is in this set -- "dr{last}" is a real format in healthcare
    # and pure noise outside it.
    industries: frozenset[str] | None = None


PATTERNS: tuple[Pattern, ...] = (
    Pattern("{first}.{last}", PRIOR_FIRST_DOT_LAST),
    Pattern("{first}", 0.15),
    Pattern("{f}{last}", 0.14),
    Pattern("{first}{last}", 0.09),
    Pattern("{first}_{last}", 0.05),
    Pattern("{last}{f}", 0.03),
    Pattern("{f}.{last}", 0.03),
    Pattern("{last}", 0.02),
    Pattern("{first}-{last}", 0.02),
    Pattern("{f}{l}", 0.01),
    Pattern("{last}.{first}", 0.01),
)

# Kept from the prototype, but scoped rather than applied everywhere.
HEALTHCARE_INDUSTRIES = frozenset({"healthcare", "medical", "dental", "veterinary", "clinic"})
INDUSTRY_PATTERNS: tuple[Pattern, ...] = (
    Pattern("dr{last}", 0.02, industries=HEALTHCARE_INDUSTRIES),
    Pattern("dr.{last}", 0.01, industries=HEALTHCARE_INDUSTRIES),
)

# Sorted by prior, so an industry pattern competes on its real frequency
# rather than being exiled behind every generic one. "dr{last}" (~0.02 in
# healthcare) belongs alongside "{last}" and "{first}-{last}", not after
# the 0.01 tail — otherwise it falls outside MAX_BLIND_GUESSES and the
# industry scoping silently does nothing. Python's sort is stable, so
# equal priors keep their declared order.
ALL_PATTERNS: tuple[Pattern, ...] = tuple(
    sorted(PATTERNS + INDUSTRY_PATTERNS, key=lambda p: p.prior, reverse=True)
)

# German convention is ue, not u: "Müller" is "mueller@" at essentially
# every German company, never "muller@". Applied before generic accent
# stripping, which would otherwise flatten it to the wrong form.
_UMLAUTS = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}

_HONORIFICS = frozenset(
    {
        "dr", "dr.", "prof", "prof.", "mr", "mr.", "mrs", "mrs.", "ms", "ms.",
        "miss", "sir", "herr", "frau",
    }
)  # fmt: skip
_CREDENTIALS = frozenset(
    {
        "dds", "dmd", "md", "do", "phd", "rn", "np", "pa", "esq", "cpa", "mba",
        "bsc", "msc", "ba", "ma", "llb", "jd", "dvm", "od", "pharmd",
        "jr", "jr.", "sr", "sr.", "ii", "iii", "iv",
    }
)  # fmt: skip

# Surname particles that belong to the surname, not a middle name --
# "Johannes Willem van der Berg" is a Berg, not a Willem.
_PARTICLES = frozenset(
    {
        "van", "von", "der", "den", "de", "del", "della", "di", "da", "dos",
        "du", "la", "le", "ter", "ten", "af", "av", "bin", "al", "mac", "mc",
    }
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class KnownPattern:
    """A domain's learned format, decoupled from the SQLAlchemy row.

    CLAUDE.md conventions: persistence models don't cross layers, so the
    resolver converts a DomainPattern row into this before calling
    generate().
    """

    pattern: str
    confirmed_count: int


@dataclass(frozen=True, slots=True)
class EmailCandidate:
    value: str
    pattern: str
    confidence: float
    rank: int


def _normalise(text: str) -> str:
    """Lowercase, expand German umlauts, strip remaining accents and
    apostrophes, drop anything that isn't email-local-part safe.

    Order matters: umlaut expansion must run before NFKD accent
    stripping, or "ü" decomposes to "u" and we generate "muller@" for a
    company that actually uses "mueller@".
    """
    lowered = text.lower()
    for source, replacement in _UMLAUTS.items():
        lowered = lowered.replace(source, replacement)

    decomposed = unicodedata.normalize("NFKD", lowered)
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    # Both a straight and a typographic (curly) apostrophe -- copy-pasted
    # names use the latter as often as the former.
    stripped = stripped.replace("'", "").replace("’", "")  # noqa: RUF001

    kept = "".join(char if (char.isalnum() or char in " -") else " " for char in stripped)
    return " ".join(kept.split())


def split_name(full_name: str) -> tuple[str, str]:
    """Split a display name into (first, last), dropping honorifics,
    trailing credentials and middle names, and keeping surname particles
    attached to the surname.

    "Dr. Jane Smith, DDS"            -> ("Jane", "Smith")
    "Johannes Willem van der Berg"   -> ("Johannes", "van der Berg")
    "Cher"                           -> ("Cher", "")
    """
    # Credentials usually arrive comma-separated ("Jane Smith, DDS").
    head = full_name.split(",")[0]
    tokens = [t for t in head.split() if t.strip()]

    while tokens and tokens[0].lower().rstrip(".") in {h.rstrip(".") for h in _HONORIFICS}:
        tokens.pop(0)
    while tokens and tokens[-1].lower() in _CREDENTIALS:
        tokens.pop()

    if not tokens:
        return "", ""
    if len(tokens) == 1:
        return tokens[0], ""

    # Walk back from the final token collecting particles, so the whole
    # compound surname stays together.
    start = len(tokens) - 1
    while start - 1 > 0 and tokens[start - 1].lower() in _PARTICLES:
        start -= 1

    return tokens[0], " ".join(tokens[start:])


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def first_name_variants(first: str) -> list[str]:
    normalised = _normalise(first)
    if not normalised:
        return []
    return _dedupe([normalised.replace(" ", "").replace("-", ""), normalised.replace(" ", "")])


def surname_variants(last: str) -> list[str]:
    """Every plausible written form of a surname.

    "van der Berg" -> "vanderberg", "berg"   (companies use both)
    "Smith-Jones"  -> "smithjones", "smith-jones"
    """
    normalised = _normalise(last)
    if not normalised:
        return [""]

    variants = [normalised.replace(" ", "").replace("-", "")]
    if "-" in normalised:
        variants.append(normalised.replace(" ", ""))
    words = normalised.split()
    if len(words) > 1:
        variants.append(words[-1].replace("-", ""))
    return _dedupe(variants)


def render_template(template: str, first: str, last: str) -> str | None:
    """Substitute one (first, last) variant pair into a template, or None
    if the result isn't a usable local part.

    Shared with learning.py, which runs it in reverse: render every
    template and see which ones reproduce a confirmed local part.
    """
    local = template.format(
        first=first,
        last=last,
        f=first[0] if first else "",
        l=last[0] if last else "",
    )
    if len(local) < MIN_LOCAL_PART_LENGTH:
        return None
    if local[0] in "._-" or local[-1] in "._-":
        return None
    if ".." in local or "__" in local or "--" in local:
        return None
    return local


def _applicable_patterns(industry: str | None) -> list[Pattern]:
    normalised_industry = (industry or "").lower()
    return [p for p in ALL_PATTERNS if p.industries is None or normalised_industry in p.industries]


def generate(
    first: str,
    last: str,
    domain: str,
    *,
    known: KnownPattern | None = None,
    industry: str | None = None,
    limit: int = MAX_BLIND_GUESSES,
) -> list[EmailCandidate]:
    """Ranked candidate addresses for a person at a domain.

    With `known` set this returns exactly ONE candidate -- the 9-to-1
    reduction this session exists for. Without it, patterns are tried in
    descending order of real-world frequency.

    Returns [] rather than falling back to blind guessing when a known
    pattern can't be rendered for this particular name (e.g. the domain
    uses {first}.{last} but we only have a mononym). Spraying ten guesses
    at a domain whose format we already know would throw away the exact
    saving this function exists to produce.
    """
    firsts = first_name_variants(first)
    lasts = surname_variants(last)
    if not firsts:
        return []

    if known is not None:
        confidence = CONF_KNOWN_CONFIRMED if known.confirmed_count >= 2 else CONF_KNOWN_SINGLE
        for last_variant in lasts:
            local = render_template(known.pattern, firsts[0], last_variant)
            if local is not None:
                return [
                    EmailCandidate(
                        value=f"{local}@{domain}",
                        pattern=known.pattern,
                        confidence=confidence,
                        rank=0,
                    )
                ]
        return []

    seen: set[str] = set()
    candidates: list[EmailCandidate] = []
    for pattern in _applicable_patterns(industry):
        for first_variant in firsts:
            for last_variant in lasts:
                local = render_template(pattern.template, first_variant, last_variant)
                if local is None:
                    continue
                value = f"{local}@{domain}"
                if value in seen:
                    continue
                seen.add(value)
                rank = len(candidates)
                candidates.append(
                    EmailCandidate(
                        value=value,
                        pattern=pattern.template,
                        confidence=CONF_BLIND_TOP if rank == 0 else CONF_BLIND_REST,
                        rank=rank,
                    )
                )
                if len(candidates) >= limit:
                    return candidates
    return candidates
