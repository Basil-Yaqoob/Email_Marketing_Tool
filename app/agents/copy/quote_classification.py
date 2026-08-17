"""Classify a harvested review quote before it is allowed near an email —
ported from `email_writer/dossier.py`'s `classify_quote` and its playbook.

This exists because a keyword-harvested `missed_call_evidence` field
contains false positives: a stored "complaint" that actually reads *"I
want to thank the girls at reception; they were very kind."* Opening a
pain email with a compliment would be humiliating, so every quote is
classified here first, and the strategist is required to return its own
`quote_verdict` (a separate, model-facing judgement — see
`types.QuoteVerdict`) grounded in this classification rather than trusting
the raw scrape.
"""

from __future__ import annotations

import enum

from app.agents.copy.types import CopyLead
from app.db.models.enums import RoleClass


class QuoteClass(enum.StrEnum):
    """The mechanical read of a harvested quote — keyword-driven, so it is
    a starting classification for the strategist to weigh, not a verdict
    in itself.
    """

    PHONE_COMPLAINT = "phone_complaint"
    SCHEDULING_COMPLAINT = "scheduling_complaint"
    PRAISE = "praise"
    MIXED = "mixed"
    UNCLEAR = "unclear"
    NONE = "none"


POSITIVE_MARKERS = (
    "thank",
    "kind",
    "perfect",
    "amazing",
    "excellent",
    "wonderful",
    "great",
    "love",
    "best",
    "highly recommend",
    "professional",
    "friendly",
    "superb",
    "caring",
    "gentle",
)
PHONE_MARKERS = (
    "no one answered",
    "nobody answered",
    "couldn't get through",
    "could not get through",
    "left a voicemail",
    "never called back",
    "didn't call back",
    "no answer",
    "on hold",
    "hard to reach",
    "difficult to reach",
    "never returned my call",
    "unanswered",
)
SCHEDULING_MARKERS = (
    "reschedul",
    "cancel",
    "waiting",
    "wait time",
    "no-show",
    "appointment",
    "double booked",
    "late",
)


def classify_quote(quote: str | None) -> QuoteClass:
    """The scraper that fills `missed_call_evidence` matches on keywords and
    catches praise as well as complaints. Classify before use.
    """
    if not quote:
        return QuoteClass.NONE
    low = quote.lower()
    phone = any(m in low for m in PHONE_MARKERS)
    sched = any(m in low for m in SCHEDULING_MARKERS)
    praise = any(m in low for m in POSITIVE_MARKERS)
    if phone:
        return QuoteClass.PHONE_COMPLAINT
    if sched and not praise:
        return QuoteClass.SCHEDULING_COMPLAINT
    if praise and not sched:
        return QuoteClass.PRAISE
    if sched and praise:
        return QuoteClass.MIXED
    return QuoteClass.UNCLEAR


QUOTE_PLAYBOOK: dict[QuoteClass, str] = {
    QuoteClass.PHONE_COMPLAINT: (
        "Strongest case. Quote a short fragment and let it speak. Do not "
        "editorialise and do not shame them -- they already know."
    ),
    QuoteClass.SCHEDULING_COMPLAINT: (
        "A real complaint, but about scheduling rather than reaching them. "
        "Open on the scheduling friction, then bridge to the calls behind it."
    ),
    QuoteClass.PRAISE: (
        "This is a COMPLIMENT, not a complaint. Do not frame it as a failing. "
        "Invert it: their front desk has a reputation worth protecting, and "
        "that reputation only reaches the callers who actually get through. "
        "This is an honest, flattering angle -- use it."
    ),
    QuoteClass.MIXED: (
        "Contains praise and friction together. Lead with the praise, name the "
        "friction lightly as the exception that costs them."
    ),
    QuoteClass.UNCLEAR: (
        "Ambiguous. If you cannot tell what it evidences, do not build the "
        "email on it. Open from an observable operational fact instead and say "
        "so in your reasoning."
    ),
    QuoteClass.NONE: "No quote on file for this lead.",
}

ROLE_ANGLE: dict[RoleClass, str] = {
    RoleClass.OWNER: "Revenue and their own time. An owner feels a missed call as lost "
    "revenue and as one more thing landing on them.",
    RoleClass.MANAGER: "Workload and stress -- phone tag, interruptions, a front "
    "desk under pressure. Do not lead with revenue; it is not "
    "their budget.",
    RoleClass.MARKETING: "A warm intro path, not the decision maker. Soften the CTA to "
    "a pass-along ask.",
    RoleClass.OTHER: "Stay neutral. Lean slightly to workload over revenue.",
}

WEDGE_FRAME = {
    "phone_only_booking": "Every booking must go through a human on a phone. "
    "Leads who will not phone simply do not book.",
    "after_hours": "Calls outside opening hours reach voicemail, and most "
    "callers ring the next business rather than leave one.",
    "missed_calls": "Customers are already saying publicly that they could not get through.",
    "no_show_reschedule": "Reschedules and no-shows churn the calendar, and "
    "each one costs a phone call to fix.",
}


def derive_wedge(lead: CopyLead) -> str:
    """`primary_wedge` is blank on some sources; infer a reasonable one from
    whatever signals are present rather than leaving targeting blank.
    Leaves an explicit value alone wherever one exists.
    """
    existing = (lead.primary_wedge or "").strip()
    if existing:
        return existing
    if classify_quote(lead.missed_call_evidence) == QuoteClass.PHONE_COMPLAINT:
        return "missed_calls"
    if lead.after_hours_gap:
        return "after_hours"
    return ""


__all__ = [
    "PHONE_MARKERS",
    "POSITIVE_MARKERS",
    "QUOTE_PLAYBOOK",
    "ROLE_ANGLE",
    "SCHEDULING_MARKERS",
    "WEDGE_FRAME",
    "QuoteClass",
    "classify_quote",
    "derive_wedge",
]
