"""ReviewResponseResolver — owners who sign their Google review replies
("- Dr. Sarah, Owner"), extracted from review text already fetched for
hook mining (Session 12).

Free coverage from data the pipeline downloads anyway: no extra request,
no extra cost. Confidence is deliberately lower than a crawled or
registry-sourced name (0.60) — a review reply is self-reported in a
semi-public forum, not independently verified, and the signature is often
just a first name.

Rejecting aggressively matters more here than anywhere else in the
person-extraction code: "- The Team" and "- Management" are extremely
common unsigned closings that are just as capitalised-word-shaped as a
real name. A false positive here means a review-reply forum handle gets
addressed as if it were the owner.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal

from app.db.models.enums import RoleClass
from app.resolvers.base import BaseResolver, Candidate, LeadContext, Tier
from app.resolvers.company.extract_person import _role_class_from_title

CONF_REVIEW_RESPONSE = 0.60

# Review replies are short and informal -- a signed-off first name alone
# ("- Sarah") is common and a strong enough signal here, unlike dense page
# markup (extract_person.py's NAME_SHAPE requires 2-3 words specifically
# to avoid noise in that denser context). The character class also
# accepts a typographic apostrophe, not just a straight one -- real
# review text typed on a phone keyboard uses it far more often, e.g. a
# signed-off "O'Brien" with a curly quote instead of a straight one.
_NAME_SHAPE = r"[A-ZÀ-Ý][a-zà-ÿ'’\-]+(?:\s+[A-ZÀ-Ý][a-zà-ÿ'’\-]+){0,2}"  # noqa: RUF001
_NAME_RE = re.compile(rf"^{_NAME_SHAPE}$")

# A trailing dash-introduced sign-off: "- Dr. Sarah, Owner" or "- John Smith".
_SIGNOFF_RE = re.compile(
    r"[-–—]\s*((?:Dr\.?\s+)?[A-ZÀ-Ý][a-zà-ÿ'’\-]+(?:\s+[A-ZÀ-Ý][a-zà-ÿ'’\-]+){0,2})"  # noqa: RUF001
    r"\s*(?:,\s*([A-Za-z][A-Za-z /]*))?\s*$"
)

# Generic sign-offs that are shaped like a name but never are — the exact
# false-positive this resolver exists to reject.
_UNSIGNED_PHRASES = frozenset(
    {
        "the team",
        "the management",
        "management",
        "customer service",
        "front desk",
        "staff",
        "the staff",
        "support team",
        "admin",
        "administration",
    }
)


@dataclass(frozen=True, slots=True)
class ReviewResponseHit:
    name: str
    title: str | None
    role_class: RoleClass


def extract_review_response(text: str) -> ReviewResponseHit | None:
    match = _SIGNOFF_RE.search(text.strip())
    if match is None:
        return None

    raw_name = match.group(1).strip()
    title = (match.group(2) or "").strip() or None

    if raw_name.lower() in _UNSIGNED_PHRASES:
        return None

    # "Dr." is a courtesy prefix, not part of the name shape being
    # validated -- strip it before the shape check, keep it in the value.
    shape_check_name = re.sub(r"^Dr\.?\s+", "", raw_name, flags=re.IGNORECASE)
    if not _NAME_RE.match(shape_check_name) or shape_check_name.lower() in _UNSIGNED_PHRASES:
        return None

    return ReviewResponseHit(name=raw_name, title=title, role_class=_role_class_from_title(title))


class ReviewResponseResolver(BaseResolver):
    name = "review_response"
    field = "person_name"
    tier = Tier.FREE
    cost_per_call = Decimal("0")
    jurisdictions: frozenset[str] | None = None  # worldwide

    async def resolve(self, ctx: LeadContext) -> list[Candidate]:
        # Session 12 (hook mining) is what actually downloads the review
        # corpus; this resolver just runs a second extraction over data
        # already fetched. Until that wiring exists, known_facts simply
        # won't carry this key and this resolver reports a clean MISS —
        # see doc/02-ROADMAP.md's Discovered work for Session 08.
        raw = ctx.known_facts.get("review_responses_json")
        if not raw:
            return []
        try:
            texts = json.loads(raw)
        except json.JSONDecodeError:
            return []

        candidates: list[Candidate] = []
        for text in texts:
            if not isinstance(text, str):
                continue
            hit = extract_review_response(text)
            if hit is None:
                continue
            candidates.append(
                Candidate(
                    value=hit.name,
                    confidence=CONF_REVIEW_RESPONSE,
                    source=self.name,
                    extra={
                        "title": hit.title,
                        "role_class": hit.role_class.value,
                    },
                )
            )
        return candidates
