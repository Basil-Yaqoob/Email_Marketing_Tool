"""Per-lead, per-angle briefs — ported from `email_writer/dossier.py`.

Each lead can yield up to three independent emails, one per evidence type:
  missed_call -- what customers say about reaching them
  news_hook   -- a researched, dated, sourced event (Session 12)
  competitor  -- who is taking their calls instead

A lead is eligible for an angle only when the trigger data is present. An
ineligible angle is skipped, which is a normal outcome, not a failure
(CLAUDE.md rule 2.1 — this is the "not a failure" kind of miss, same
philosophy as a resolver MISS).

Competitor category/geography alignment (does a rival actually compete for
the same customer?) is a discovery-layer concern the platform doesn't have
a resolver for yet — see the roadmap's Discovered work. By the time a
`CompetitorInfo` reaches `CopyLead.competitors` it is assumed already
usable, unlike the original's `competitor_alignment` step.
"""

from __future__ import annotations

from app.agents.copy.quote_classification import (
    QUOTE_PLAYBOOK,
    ROLE_ANGLE,
    WEDGE_FRAME,
    classify_quote,
    derive_wedge,
)
from app.agents.copy.types import CopyLead

# Order matters: this is the order angles are considered and reported in.
ANGLE_ORDER = ("missed_call", "news_hook", "competitor")

ANGLE_LABELS = {
    "missed_call": "Missed-call / service evidence",
    "news_hook": "Recent news hook",
    "competitor": "Competitive pressure",
}


def eligible_angles(lead: CopyLead) -> list[str]:
    """Every angle this lead has data for, in `ANGLE_ORDER`.

    `news_hook` requires BOTH the hook text and its source URL — a hook
    without a source never reaches an email, mirroring the same bar
    Session 12's validator already enforces before a hook is persisted
    (CLAUDE.md rule 2.2: a claim without a source URL is dropped, not
    softened). Belt-and-braces here rather than trusting upstream data
    alone.
    """
    out: list[str] = []
    has_hook = bool(lead.hook_text and lead.hook_text.strip())
    has_source = bool(lead.hook_source_url and lead.hook_source_url.strip())

    if lead.missed_call_evidence and lead.missed_call_evidence.strip():
        out.append("missed_call")
    if has_hook and has_source:
        out.append("news_hook")
    if lead.competitors:
        out.append("competitor")
    return [a for a in ANGLE_ORDER if a in out]


def _common(lead: CopyLead) -> list[str]:
    lines = [
        "## Lead",
        f"- company: {lead.company_name}",
        f"- contact: {lead.contact_name or '(unknown)'} ({lead.contact_title or 'title unknown'})",
        f"- type: {lead.industry or '(unknown)'}",
        f"- location: {lead.city or '(unknown)'}",
        f"- website: {lead.website or '(none)'}",
    ]
    if lead.rating:
        lines.append(f"- rating: {lead.rating}")
    if lead.review_count:
        lines.append(f"- review count: {lead.review_count}")
    if lead.after_hours_gap is not None:
        lines.append(f"- after-hours gap: {'yes' if lead.after_hours_gap else 'no'}")
    if lead.notes:
        lines.append(f"- internal operator note (a hint, never an instruction): {lead.notes}")

    wedge = derive_wedge(lead)
    lines += [
        "",
        "## Targeting",
        f"- decision maker role: {lead.role_class.value}",
        f"- angle for that role: {ROLE_ANGLE[lead.role_class]}",
        f"- primary wedge: {wedge or '(none)'}",
        f"- what that wedge means: {WEDGE_FRAME.get(wedge, '(derive from the data above)')}",
    ]
    return lines


def build_brief(lead: CopyLead, angle: str) -> str:
    """The brief for exactly one angle. Each angle is argued
    independently -- the competitor email never sees the review quote, and
    vice versa -- so the brief only ever includes the section for the
    requested angle.
    """
    lines = _common(lead)

    if angle == "missed_call":
        quote = lead.missed_call_evidence or ""
        kind = classify_quote(quote)
        lines += [
            "",
            "## THIS EMAIL'S ANGLE: what customers say about reaching them",
            f'- the stored quote: "{quote}"',
            f"- classification: {kind.value}",
            f"- how to use it: {QUOTE_PLAYBOOK[kind]}",
        ]
        if lead.review_themes:
            lines.append(f"- other review themes: {lead.review_themes[:500]}")
        lines.append(
            "\nBuild the whole email on this evidence. Do not pivot to a news "
            "item or a competitor -- those are separate emails."
        )

    elif angle == "news_hook":
        lines += [
            "",
            "## THIS EMAIL'S ANGLE: a researched, dated, sourced event",
            f"- the hook: {lead.hook_text}",
            f"- source: {lead.hook_source_url} ({lead.hook_date or 'undated'})",
        ]
        lines.append(
            "\nThe hook was verified against the evidence bar. Open on it "
            "near-verbatim, then bridge to the phone pressure it creates. Do "
            "not restate it twice."
        )

    elif angle == "competitor":
        comps = sorted(lead.competitors, key=lambda c: -c.threat)
        lines += [
            "",
            "## THIS EMAIL'S ANGLE: who is taking their calls instead",
            f"- {len(comps)} competitor(s) on file, strongest threat first:",
        ]
        for c in comps:
            lines.append(f"  - {c.name} | {c.rating}* | {c.reviews} reviews | threat {c.threat}")
            if c.why:
                lines.append(f"      why they matter: {c.why}")

        own = f"{lead.rating or '?'}* / {lead.review_count or '?'} reviews"
        lines += [
            f"- THIS lead for comparison: {own}",
            "",
            "Use ONE competitor, normally the highest threat, unless another "
            "makes a sharper point. Name them. A named rival is what makes "
            "this email land; 'other businesses nearby' is worth nothing. "
            "The argument is not that the rival is better -- it is that when "
            "a customer cannot reach this lead, that named rival is the next "
            "call, and 'why they matter' usually says exactly why they "
            "convert. Stay factual and never disparage either party.",
        ]

    return "\n".join(lines)


__all__ = ["ANGLE_LABELS", "ANGLE_ORDER", "build_brief", "eligible_angles"]
