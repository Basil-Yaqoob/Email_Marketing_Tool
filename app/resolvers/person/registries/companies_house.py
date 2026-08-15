"""CompaniesHouseResolver — every UK limited company's officers, free.

By law, every UK registered company files its directors with Companies
House, and Companies House publishes them through a free REST API. That
makes this the highest-yield, lowest-risk source in the product: no ToS
grey area, no scraping, structured names with roles and appointment dates.

Flow: search by name -> score against what we already know about the lead
-> if the best match clears MATCH_THRESHOLD, fetch its officers -> keep
only active (non-resigned), non-corporate officers -> map role to
confidence and role_class.

Auth is HTTP Basic with the API key as username and an empty password —
see https://developer-specs.company-information.service.gov.uk/.
"""

from __future__ import annotations

import base64
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

API_BASE = "https://api.company-information.service.gov.uk"
SEARCH_URL = f"{API_BASE}/search/companies"
OFFICERS_URL_TMPL = f"{API_BASE}/company/{{number}}/officers"
FIND_AND_UPDATE_URL_TMPL = (
    "https://find-and-update.company-information.service.gov.uk/company/{number}"
)

# Companies House officer_role values that represent another company acting
# as director/secretary rather than a real person -- see the plan's gotcha:
# "Filter those out — they have no personal email."
_CORPORATE_ROLE_PREFIX = "corporate-"

# role -> (role_class, confidence). "director" is handled separately since
# its confidence depends on whether it's the sole active director.
_ROLE_MAP: dict[str, tuple[RoleClass, float]] = {
    "llp-designated-member": (RoleClass.OWNER, 0.85),
    "llp-member": (RoleClass.OWNER, 0.60),
    "secretary": (RoleClass.OTHER, 0.50),
    "nominee-director": (RoleClass.OTHER, 0.30),
    "nominee-secretary": (RoleClass.OTHER, 0.30),
    "managing-officer": (RoleClass.MANAGER, 0.55),
    "member-of-a-management-organ": (RoleClass.OTHER, 0.40),
    "member-of-a-supervisory-organ": (RoleClass.OTHER, 0.40),
    "member-of-an-administrative-organ": (RoleClass.OTHER, 0.40),
}

SOLE_DIRECTOR_CONFIDENCE = 0.90
MULTI_DIRECTOR_CONFIDENCE = 0.75


def _extract_postcode(address_snippet: str | None) -> str | None:
    """Companies House search results give a single free-text
    address_snippet, not structured fields — the postcode is whatever
    all-caps-ish token at the end looks like a UK postcode.
    """
    if not address_snippet:
        return None
    tokens = address_snippet.replace(",", " ").split()
    if not tokens:
        return None
    # A UK postcode is the last one or two tokens, e.g. "EC1A 1BB" or "SW1A1AA".
    candidate = " ".join(tokens[-2:]) if len(tokens) >= 2 else tokens[-1]
    if any(char.isdigit() for char in candidate):
        return candidate
    return None


class CompaniesHouseResolver(BaseResolver):
    name = "companies_house"
    field = "person_name"
    tier = Tier.FREE
    cost_per_call = Decimal("0")
    jurisdictions: frozenset[str] | None = frozenset({"GB"})

    def __init__(self, *, http: HttpClient, api_key: str) -> None:
        self._http = http
        self._api_key = api_key

    def _auth_header(self) -> dict[str, str]:
        token = base64.b64encode(f"{self._api_key}:".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    async def resolve(self, ctx: LeadContext) -> list[Candidate]:
        query = CompanyQuery(
            name=ctx.company_name,
            country_code=ctx.country_code,
            postcode=ctx.known_facts.get("postcode"),
            address=ctx.known_facts.get("address"),
        )
        match = await self._find_match(query)
        if match is None:
            return []

        officers = await self._fetch_active_officers(match.company_number)
        return self._officers_to_candidates(officers, match)

    async def _find_match(self, query: CompanyQuery) -> RegistryMatch | None:
        response = await self._http.get(
            f"{SEARCH_URL}?q={quote(query.name)}",
            extra_headers=self._auth_header(),
            respect_robots=False,  # API call, not a page fetch — same as HttpClient.post()
        )
        data = json.loads(response.text)

        best: RegistryMatch | None = None
        for item in data.get("items", []):
            company_number = item.get("company_number")
            title = item.get("title")
            if not company_number or not title:
                continue
            candidate = RegistryCompany(
                company_number=company_number,
                name=title,
                entity_type=item.get("company_type", "unknown"),
                postcode=_extract_postcode(item.get("address_snippet")),
                raw=item,
            )
            match = score_company_match(query, candidate)
            if best is None or match.match_score > best.match_score:
                best = match

        if best is None or best.match_score < MATCH_THRESHOLD:
            return None
        return best

    async def _fetch_active_officers(self, company_number: str) -> list[dict[str, Any]]:
        response = await self._http.get(
            OFFICERS_URL_TMPL.format(number=company_number),
            extra_headers=self._auth_header(),
            respect_robots=False,  # API call, not a page fetch — same as HttpClient.post()
        )
        data = json.loads(response.text)
        return [item for item in data.get("items", []) if item.get("resigned_on") is None]

    def _officers_to_candidates(
        self, officers: list[dict[str, Any]], match: RegistryMatch
    ) -> list[Candidate]:
        active_directors = sum(1 for o in officers if o.get("officer_role") == "director")
        sole_director = active_directors == 1
        source_url = FIND_AND_UPDATE_URL_TMPL.format(number=match.company_number)

        candidates: list[Candidate] = []
        for officer in officers:
            role = officer.get("officer_role", "")
            if not role or role.startswith(_CORPORATE_ROLE_PREFIX):
                continue

            if role == "director":
                role_class, confidence = (
                    RoleClass.OWNER,
                    SOLE_DIRECTOR_CONFIDENCE if sole_director else MULTI_DIRECTOR_CONFIDENCE,
                )
            elif role in _ROLE_MAP:
                role_class, confidence = _ROLE_MAP[role]
            else:
                # An officer_role we don't have a mapping for yet -- record
                # it at low confidence rather than dropping it silently, so
                # it's visible in telemetry instead of vanishing.
                role_class, confidence = RoleClass.OTHER, 0.30

            raw_name = officer.get("name", "")
            if not raw_name:
                continue
            parsed = parse_officer_name(raw_name)

            candidates.append(
                Candidate(
                    value=parsed.full,
                    confidence=confidence,
                    source=self.name,
                    source_url=source_url,
                    extra={
                        "role_class": role_class.value,
                        "officer_role": role,
                        "first_name": parsed.first,
                        "middle_name": parsed.middle,
                        "last_name": parsed.last,
                        "company_number": match.company_number,
                        "entity_type": match.entity_type,
                    },
                )
            )
        return candidates
