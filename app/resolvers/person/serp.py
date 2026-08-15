"""SERPPersonResolver — decision-maker discovery from search-result
snippets, never a linkedin.com fetch.

LinkedIn has the data (name, title, employer) but scraping it is the wrong
move: hiQ v. LinkedIn settled the CFAA question (public scraping isn't
"unauthorised access") but not the contract one — LinkedIn won on breach
of its User Agreement and has sued scrapers over exactly this. Cookie-based
access risks the user's own account.

None of that matters here, because the data we need is already sitting in
the search-result snippet: `site:linkedin.com/in "CEO" "Company"` returns
`Jane Smith - CEO - Northgate Dental | LinkedIn` from the search index
itself. We read that string. We never issue a request to linkedin.com —
see tests/unit/test_no_linkedin_fetch.py for the structural guarantee.

Query templates are tried in order and stop at the first one that yields
an accepted match — "accepted" meaning the snippet's company name actually
scores against this lead's company (score_company_match, reused from
Session 07) rather than being a same-named business somewhere else, and
the extracted text is name-shaped, not a stray phrase.

All candidates from this resolver share independence_key="search_index":
two query templates hitting the same underlying search index are not two
independent pieces of evidence, and must not be allowed to stack
confidence (Session 03's merge rule) — this is precisely the case that
exists for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from app.net.search import SearchBackend
from app.resolvers.base import BaseResolver, Candidate, LeadContext, Tier
from app.resolvers.company.extract_person import _role_class_from_title
from app.resolvers.person.registries.base import (
    MATCH_THRESHOLD,
    CompanyQuery,
    RegistryCompany,
    score_company_match,
)

# SERP is medium confidence by design (see the plan's gotcha): a snippet
# can name someone who *used to* work there, or a same-named business in a
# different city that still cleared score_company_match. It should be
# corroborated by another source, not trusted alone.
CONF_SERP = 0.70

INDEPENDENCE_KEY = "search_index"

QUERY_TEMPLATES: tuple[str, ...] = (
    'site:linkedin.com/in "{company}" ("CEO" OR "Owner" OR "Founder")',
    'site:linkedin.com/in "{company}" "{city}"',
    '"{company}" ("CEO" OR "owner" OR "managing director") -site:linkedin.com',
    '"{company}" "founded by"',
)

# The en dash in this character class is matched deliberately, not a
# typo: search-result titles routinely use one instead of a hyphen
# ("Jane Smith [en dash] Owner [en dash] Company").
_TRAILING_LINKEDIN_RE = re.compile(r"\s*[|\-–]\s*linkedin\s*$", re.IGNORECASE)  # noqa: RUF001
_DASH_RE = re.compile(r"\s*[-–]\s*")  # noqa: RUF001
_PIPE_RE = re.compile(r"\s*\|\s*")

# Two to three capitalised words -- same shape family as Session 06's
# extract_person, kept intentionally simple here since snippet text is
# already short and low-noise, unlike dense page markup.
_NAME_SHAPE = r"[A-ZÀ-Ý][a-zà-ÿ'’\-]+(?:\s+[A-ZÀ-Ý][a-zà-ÿ'’\-]+){1,2}"  # noqa: RUF001
_NAME_RE = re.compile(rf"^{_NAME_SHAPE}$")


def _looks_like_person_name(text: str) -> bool:
    return bool(_NAME_RE.match(text.strip()))


@dataclass(frozen=True, slots=True)
class SnippetHit:
    name: str
    title: str | None
    company: str


def parse_linkedin_snippet(title: str, *, company_hint: str | None = None) -> SnippetHit | None:
    """Parses the two shapes a LinkedIn search-result title reliably
    takes, dash-separated (with either a hyphen or an en dash -- search
    indexes use both) or pipe-separated:

        "Jane Smith - CEO - Northgate Dental | LinkedIn"
        "Northgate Dental | Jane Smith | LinkedIn"

    Returns None for anything else — a parse failure is a MISS, not a
    crash (see the plan's gotcha on snippet drift).

    `company_hint` (the lead's known company name) disambiguates the
    pipe-separated shape: "Northgate Dental" and "Jane Smith" are both
    two-capitalised-word phrases, so the name-shape check alone can't
    tell a business name from a person's name — score_company_match
    (reused from Session 07) against the hint can.
    """
    stripped = _TRAILING_LINKEDIN_RE.sub("", title).strip()
    if not stripped or stripped == title.strip():
        return None  # no "| LinkedIn" / "- LinkedIn" suffix at all

    dash_parts = [p for p in _DASH_RE.split(stripped) if p]
    if len(dash_parts) == 3:
        name, role_title, company = dash_parts
        if _looks_like_person_name(name):
            return SnippetHit(name=name.strip(), title=role_title.strip(), company=company.strip())

    pipe_parts = [p for p in _PIPE_RE.split(stripped) if p]
    if len(pipe_parts) == 2:
        first, second = pipe_parts
        first_is_name = _looks_like_person_name(first)
        second_is_name = _looks_like_person_name(second)
        if first_is_name and not second_is_name:
            return SnippetHit(name=first.strip(), title=None, company=second.strip())
        if second_is_name and not first_is_name:
            return SnippetHit(name=second.strip(), title=None, company=first.strip())
        if first_is_name and second_is_name and company_hint:
            if _company_matches(first, company_hint):
                return SnippetHit(name=second.strip(), title=None, company=first.strip())
            if _company_matches(second, company_hint):
                return SnippetHit(name=first.strip(), title=None, company=second.strip())

    return None


def _company_matches(snippet_company: str, lead_company_name: str) -> bool:
    query = CompanyQuery(name=lead_company_name, country_code="")
    candidate = RegistryCompany(company_number="", name=snippet_company, entity_type="unknown")
    return score_company_match(query, candidate).match_score >= MATCH_THRESHOLD


class SERPPersonResolver(BaseResolver):
    name = "serp_person"
    field = "person_name"
    tier = Tier.FREE
    cost_per_call = Decimal("0")
    jurisdictions: frozenset[str] | None = None  # worldwide

    def __init__(self, *, search: SearchBackend) -> None:
        self._search = search

    async def resolve(self, ctx: LeadContext) -> list[Candidate]:
        city = ctx.known_facts.get("city")
        candidates: list[Candidate] = []

        for template in QUERY_TEMPLATES:
            if "{city}" in template and not city:
                continue  # can't fill this template without data we don't have

            query = template.format(company=ctx.company_name, city=city or "")
            results = await self._search.search(query, limit=10)

            for result in results:
                hit = parse_linkedin_snippet(result.title, company_hint=ctx.company_name)
                if hit is None:
                    continue
                if not _company_matches(hit.company, ctx.company_name):
                    continue
                candidates.append(
                    Candidate(
                        value=hit.name,
                        confidence=CONF_SERP,
                        source=self.name,
                        source_url=result.url,
                        independence_key=INDEPENDENCE_KEY,
                        extra={
                            "title": hit.title,
                            "role_class": _role_class_from_title(hit.title).value,
                            "query": query,
                        },
                    )
                )

            if candidates:
                # Tried in order until confident: a template that already
                # produced an accepted match is enough -- don't spend more
                # search calls confirming what we already have.
                break

        return candidates
