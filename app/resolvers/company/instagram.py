"""InstagramEmailResolver — business-profile contact email via the search
index, never a request to instagram.com.

Meta actively fights direct access: `recent_news_agent/channels.py` in the
prior work already probed all three social platforms directly and found
Instagram returns 429, Facebook's mbasic returns 400, and LinkedIn sits
behind an auth wall — concluding the search index is "the actual channel"
for all three. This resolver promotes that finding to a first-class
source rather than re-litigating a wall that's already been measured.

Instagram business accounts often put a contact email directly in their
bio, and that bio text is frequently indexed and shown in the search
snippet. Confidence is 0.65 — lower than a crawled mailto: link (0.90,
Session 06) because bios go stale and a business can change or close
without updating one.
"""

from __future__ import annotations

from decimal import Decimal

from app.net.search import SearchBackend
from app.resolvers.base import BaseResolver, Candidate, LeadContext, Tier
from app.resolvers.company.extract_email import EMAIL_RE, _is_bad

CONF_INSTAGRAM = 0.65
INDEPENDENCE_KEY = "search_index"


def _email_from_text(text: str) -> str | None:
    match = EMAIL_RE.search(text)
    if match is None:
        return None
    address = match.group(0)
    if _is_bad(address):
        return None
    return address.lower()


class InstagramEmailResolver(BaseResolver):
    name = "instagram_email"
    field = "email"
    tier = Tier.FREE
    cost_per_call = Decimal("0")
    jurisdictions: frozenset[str] | None = None  # worldwide

    def __init__(self, *, search: SearchBackend) -> None:
        self._search = search

    async def resolve(self, ctx: LeadContext) -> list[Candidate]:
        query = f'site:instagram.com "{ctx.company_name}"'
        results = await self._search.search(query, limit=10)

        candidates: list[Candidate] = []
        for result in results:
            email = _email_from_text(result.snippet) or _email_from_text(result.title)
            if email is None:
                continue
            candidates.append(
                Candidate(
                    value=email,
                    confidence=CONF_INSTAGRAM,
                    source=self.name,
                    source_url=result.url,
                    independence_key=INDEPENDENCE_KEY,
                    extra={"strategy": "instagram_bio"},
                )
            )
        return candidates
