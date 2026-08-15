"""EdgarResolver — US public companies only, via SEC EDGAR.

Deliberately narrow, per the plan: "low hit rate for SMBs; keep it cheap
and don't over-invest." Most cold-email leads are private SMBs that never
appear on EDGAR at all — this resolver exists for the minority that do,
not as a general-purpose US company database.

Two free, structured lookups, no scraping:

  1. CIK lookup — SEC publishes the full ticker/CIK/name list as a single
     static JSON file. Matched the same way as every other registry
     (score_company_match), just without a postcode signal to boost with,
     since the ticker file carries no address.
  2. Officer names — SEC's insider-ownership filings (Forms 3/4) are XML
     and list each reporting owner's name plus whether they're a director
     and/or officer, with their title. We read only the single most recent
     Form 3/4 on file, which is enough to surface who's currently an
     officer/director without fetching a company's entire filing history.

SEC blocks anonymous traffic — every request carries a descriptive
User-Agent naming the application and a contact address, per
https://www.sec.gov/os/webmaster-faq#developers.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from xml.etree import ElementTree

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

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL_TMPL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE_URL_TMPL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"

_INSIDER_FORMS = frozenset({"3", "4", "5"})

_OFFICER_OWNER_TITLES = ("chief executive", "ceo", "president", "founder", "chairman", "owner")

DIRECTOR_CONFIDENCE = 0.70
OFFICER_CONFIDENCE = 0.65


class EdgarResolver(BaseResolver):
    name = "edgar"
    field = "person_name"
    tier = Tier.FREE
    cost_per_call = Decimal("0")
    jurisdictions: frozenset[str] | None = frozenset({"US"})

    def __init__(self, *, http: HttpClient, contact: str) -> None:
        """`contact` is an application name and email, e.g.
        "EmailMarketingTool research@example.com" — required by SEC's fair
        access policy, not optional.
        """
        self._http = http
        self._contact = contact

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": f"EmailMarketingTool/1.0 ({self._contact})"}

    async def resolve(self, ctx: LeadContext) -> list[Candidate]:
        query = CompanyQuery(name=ctx.company_name, country_code=ctx.country_code)
        match_and_cik = await self._find_match(query)
        if match_and_cik is None:
            return []
        match, cik = match_and_cik

        filing = await self._most_recent_insider_filing(cik)
        if filing is None:
            return []

        owners = await self._fetch_reporting_owners(filing)
        return self._owners_to_candidates(owners, match, filing_url=filing["url"])

    async def _find_match(self, query: CompanyQuery) -> tuple[RegistryMatch, int] | None:
        response = await self._http.get(
            TICKERS_URL, extra_headers=self._headers(), respect_robots=False
        )
        data = json.loads(response.text)

        best: RegistryMatch | None = None
        best_cik: int | None = None
        for entry in data.values():
            cik = entry.get("cik_str")
            title = entry.get("title")
            if cik is None or not title:
                continue
            candidate = RegistryCompany(
                company_number=str(cik), name=title, entity_type="sec_registrant"
            )
            match = score_company_match(query, candidate)
            if best is None or match.match_score > best.match_score:
                best = match
                best_cik = cik

        if best is None or best.match_score < MATCH_THRESHOLD or best_cik is None:
            return None
        return best, best_cik

    async def _most_recent_insider_filing(self, cik: int) -> dict[str, str] | None:
        response = await self._http.get(
            SUBMISSIONS_URL_TMPL.format(cik=cik),
            extra_headers=self._headers(),
            respect_robots=False,
        )
        data = json.loads(response.text)
        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        accessions = recent.get("accessionNumber", [])
        documents = recent.get("primaryDocument", [])

        for form, accession, document in zip(forms, accessions, documents, strict=False):
            if form not in _INSIDER_FORMS:
                continue
            url = ARCHIVE_URL_TMPL.format(
                cik=cik, accession=accession.replace("-", ""), document=document
            )
            return {"form": form, "accession": accession, "document": document, "url": url}
        return None

    async def _fetch_reporting_owners(self, filing: dict[str, str]) -> list[dict[str, Any]]:
        # Archive filing documents are structured data (XML), fetched by a
        # direct, known URL from the submissions index -- not a crawl, so
        # the same "API call, not a page fetch" reasoning applies.
        response = await self._http.get(
            filing["url"], extra_headers=self._headers(), respect_robots=False
        )
        return _parse_reporting_owners(response.text)

    def _owners_to_candidates(
        self, owners: list[dict[str, Any]], match: RegistryMatch, *, filing_url: str
    ) -> list[Candidate]:
        candidates: list[Candidate] = []
        for owner in owners:
            raw_name = owner.get("name", "")
            if not raw_name:
                continue

            role_class, confidence = _classify_owner(owner)
            if role_class is None:
                # A ten-percent-owner with no officer or director role is
                # an investor, not a decision maker for outreach.
                continue

            parsed = parse_officer_name(raw_name)
            candidates.append(
                Candidate(
                    value=parsed.full,
                    confidence=confidence,
                    source=self.name,
                    source_url=filing_url,
                    extra={
                        "role_class": role_class.value,
                        "officer_title": owner.get("officer_title"),
                        "first_name": parsed.first,
                        "middle_name": parsed.middle,
                        "last_name": parsed.last,
                        "cik": match.company_number,
                        "entity_type": match.entity_type,
                    },
                )
            )
        return candidates


def _classify_owner(owner: dict[str, Any]) -> tuple[RoleClass | None, float]:
    title = (owner.get("officer_title") or "").lower()
    if owner.get("is_officer"):
        if any(k in title for k in _OFFICER_OWNER_TITLES):
            return RoleClass.OWNER, OFFICER_CONFIDENCE
        return RoleClass.MANAGER, OFFICER_CONFIDENCE
    if owner.get("is_director"):
        return RoleClass.OWNER, DIRECTOR_CONFIDENCE
    return None, 0.0


def _parse_reporting_owners(xml_text: str) -> list[dict[str, Any]]:
    try:
        # SEC's own XML, not third-party HTML -- no injection surface here.
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return []

    owners: list[dict[str, Any]] = []
    for owner_el in root.findall(".//reportingOwner"):
        name_el = owner_el.find("./reportingOwnerId/rptOwnerName")
        name = (name_el.text or "").strip() if name_el is not None else ""
        if not name:
            continue

        rel_el = owner_el.find("./reportingOwnerRelationship")
        is_officer = _bool_field(rel_el, "isOfficer")
        is_director = _bool_field(rel_el, "isDirector")
        title_el = rel_el.find("officerTitle") if rel_el is not None else None
        officer_title = (
            (title_el.text or "").strip() if title_el is not None and title_el.text else None
        )

        owners.append(
            {
                "name": name,
                "is_officer": is_officer,
                "is_director": is_director,
                "officer_title": officer_title,
            }
        )
    return owners


def _bool_field(parent: ElementTree.Element | None, tag: str) -> bool:
    if parent is None:
        return False
    el = parent.find(tag)
    if el is None or el.text is None:
        return False
    return el.text.strip() in ("1", "true", "True")
