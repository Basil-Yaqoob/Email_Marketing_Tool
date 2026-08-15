"""Which registry serves which country — data, not `if` statements.

New country coverage should mean adding a row here and building the
resolver, not touching every call site that currently branches on
country_code. The FREE registries are gated at the class level too (each
resolver sets its own `jurisdictions`), so this map isn't the only thing
protecting cost — it's what lets the rest of the codebase (routing, UI,
docs) answer "do we have anything for this country?" without importing
every resolver module.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RegistryCoverage:
    resolver_name: str
    tier: str  # "free" | "metered" — mirrors Tier but kept as a plain
    # string here so this module stays free of imports beyond the stdlib.
    countries: frozenset[str] | None  # None = worldwide


# Built registries, in the order they're worth trying for a given country.
COVERAGE: tuple[RegistryCoverage, ...] = (
    RegistryCoverage(resolver_name="companies_house", tier="free", countries=frozenset({"GB"})),
    RegistryCoverage(resolver_name="edgar", tier="free", countries=frozenset({"US"})),
    RegistryCoverage(resolver_name="opencorporates", tier="metered", countries=None),
)

# Documented, not built. Companies House-shaped free registries exist in
# all of these — worth adding when a real campaign needs the coverage:
#
#   India     — Ministry of Corporate Affairs (MCA21), free company search
#   Australia — ASIC, free ABN/ACN lookup, officer data behind a paid extract
#   Canada    — Corporations Canada federal registry, free; each province
#               also runs its own (Ontario, BC, ...) with inconsistent APIs
#   EU        — BRIS (Business Registers Interconnection System), links
#               national registries; coverage and officer-data depth vary
#               a lot by member state
UNBUILT_COVERAGE_NOTES: tuple[str, ...] = (
    "India: Ministry of Corporate Affairs (MCA21)",
    "Australia: ASIC free ABN/ACN lookup",
    "Canada: Corporations Canada (federal) + per-province registries",
    "EU: BRIS (Business Registers Interconnection System)",
)


def registries_for(country_code: str) -> list[RegistryCoverage]:
    """Every registry applicable to a country, free tiers first."""
    return [c for c in COVERAGE if c.countries is None or country_code in c.countries]


def has_free_registry(country_code: str) -> bool:
    return any(c.tier == "free" for c in registries_for(country_code))
