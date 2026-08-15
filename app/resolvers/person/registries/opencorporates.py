"""OpenCorporatesResolver — same shape as Companies House, but worldwide
and METERED.

OpenCorporates aggregates 200M+ companies across many jurisdictions, which
is exactly what makes it useful outside the UK — and exactly why it can't
be FREE tier: its API requires a token, and its free allowance is for
non-commercial use only (read the licence before pointing this at client
work). The waterfall executor only reaches METERED resolvers when the free
tier left confidence below threshold and the caller explicitly opted in
(`allow_metered=True`), so this fires rarely by design.

Officer position strings here are free text ("Director", "CEO", "Company
Secretary", ...) rather than a fixed vocabulary like Companies House's
officer_role, so role classification is a keyword match rather than a
lookup table.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from urllib.parse import quote

from app.db.models.enums import RoleClass
from app.net.client import HttpClient
from app.resolvers.base import BaseResolver, Candidate, LeadContext, Tier
from app.resolvers.person.registries.base import (
    MATCH_THRESHOLD,
    CompanyQuery,
    RegistryCompany,
    RegistryMatch,
    parse_officer_name,
    score_company_match,
)

API_BASE = "https://api.opencorporates.com/v0.4"

# OpenCorporates' published per-call price for the paid search tier at time
# of writing. Update if their pricing changes — see PlacesBudget for the
# pattern this cost feeds if a budget guard is added later.
COST_PER_REQUEST = Decimal("0.01")

_OWNER_KEYWORDS = ("owner", "proprietor", "director", "partner", "founder", "principal", "member")
_MANAGER_KEYWORDS = ("manager", "management", "ceo", "president", "chief executive")
_SECRETARY_KEYWORDS = ("secretary",)

DEFAULT_CONFIDENCE = 0.60


def _role_class_from_position(position: str) -> RoleClass:
    lowered = position.lower()
    if any(k in lowered for k in _OWNER_KEYWORDS):
        return RoleClass.OWNER
    if any(k in lowered for k in _MANAGER_KEYWORDS):
        return RoleClass.MANAGER
    if any(k in lowered for k in _SECRETARY_KEYWORDS):
        return RoleClass.OTHER
    return RoleClass.OTHER


class OpenCorporatesResolver(BaseResolver):
    name = "opencorporates"
    field = "person_name"
    tier = Tier.METERED
    cost_per_call = COST_PER_REQUEST
    jurisdictions: frozenset[str] | None = None  # worldwide

    def __init__(self, *, http: HttpClient, api_key: str | None) -> None:
        self._http = http
        self._api_key = api_key

    async def applicable(self, ctx: LeadContext) -> bool:
        """No key configured -> this source degrades explicitly (returns
        False, SKIPPED in telemetry) rather than crashing the batch over an
        optional integration nobody set up. CLAUDE.md 2.1: a stage that
        cannot do its job raises or reports absence loudly — it never
        pretends the source ran and simply found nothing.
        """
        if not self._api_key:
            return False
        return await super().applicable(ctx)

    async def resolve(self, ctx: LeadContext) -> list[Candidate]:
        query = CompanyQuery(
            name=ctx.company_name,
            country_code=ctx.country_code,
            postcode=ctx.known_facts.get("postcode"),
            address=ctx.known_facts.get("address"),
        )
        match, jurisdiction_code = await self._find_match(query)
        if match is None or jurisdiction_code is None:
            return []

        officers = await self._fetch_active_officers(jurisdiction_code, match.company_number)
        return self._officers_to_candidates(officers, match)

    async def _find_match(self, query: CompanyQuery) -> tuple[RegistryMatch | None, str | None]:
        response = await self._http.get(
            f"{API_BASE}/companies/search?q={quote(query.name)}&api_token={self._api_key}",
            respect_robots=False,  # API call, not a page fetch — same as HttpClient.post()
        )
        data = json.loads(response.text)
        companies = data.get("results", {}).get("companies", [])

        best: RegistryMatch | None = None
        best_jurisdiction: str | None = None
        for entry in companies:
            company = entry.get("company", {})
            company_number = company.get("company_number")
            name = company.get("name")
            jurisdiction_code = company.get("jurisdiction_code")
            if not company_number or not name or not jurisdiction_code:
                continue
            candidate = RegistryCompany(
                company_number=company_number,
                name=name,
                entity_type=company.get("company_type") or "unknown",
                postcode=_postcode_from(company),
                raw=company,
            )
            match = score_company_match(query, candidate)
            if best is None or match.match_score > best.match_score:
                best = match
                best_jurisdiction = jurisdiction_code

        if best is None or best.match_score < MATCH_THRESHOLD:
            return None, None
        return best, best_jurisdiction

    async def _fetch_active_officers(
        self, jurisdiction_code: str, company_number: str
    ) -> list[dict[str, Any]]:
        response = await self._http.get(
            f"{API_BASE}/companies/{jurisdiction_code}/{company_number}/officers"
            f"?api_token={self._api_key}",
            respect_robots=False,  # API call, not a page fetch — same as HttpClient.post()
        )
        data = json.loads(response.text)
        results = data.get("results", {}).get("officers", [])
        officers = [entry.get("officer", {}) for entry in results]
        return [o for o in officers if o.get("end_date") is None]

    def _officers_to_candidates(
        self, officers: list[dict[str, Any]], match: RegistryMatch
    ) -> list[Candidate]:
        source_url = f"https://opencorporates.com/companies/search?q={quote(match.registered_name)}"
        candidates: list[Candidate] = []
        for officer in officers:
            raw_name = officer.get("name", "")
            if not raw_name:
                continue
            position = officer.get("position") or ""
            role_class = _role_class_from_position(position) if position else RoleClass.OTHER
            parsed = parse_officer_name(raw_name)

            candidates.append(
                Candidate(
                    value=parsed.full,
                    confidence=DEFAULT_CONFIDENCE,
                    source=self.name,
                    source_url=source_url,
                    extra={
                        "role_class": role_class.value,
                        "position": position,
                        "first_name": parsed.first,
                        "middle_name": parsed.middle,
                        "last_name": parsed.last,
                        "company_number": match.company_number,
                        "entity_type": match.entity_type,
                    },
                )
            )
        return candidates


def _postcode_from(company: dict[str, Any]) -> str | None:
    address = company.get("registered_address") or {}
    postcode = address.get("postal_code")
    if postcode:
        return str(postcode)
    return None
