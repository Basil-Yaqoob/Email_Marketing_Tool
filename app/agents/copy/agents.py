"""The three-agent pipeline: strategist -> copywriter -> critic, with
revision — ported from `email_writer/agents.py`.

**A draft passes only if the model approves it AND the mechanical scan
(`quality.scan`) is clean.** The model gets no vote on whether an em dash
is present (CLAUDE.md rule 2.5) — see the `passed` computation in
`write_email` below; this is the load-bearing line in the whole module.

Revision keeps the best draft seen, not the last one — a revision can come
back worse than what it replaced, or empty, and shipping the final attempt
regardless threw away most drafts in testing on the original. `rank()`
below is that tie-break: clean-mechanical-scan beats dirty regardless of
score, then higher score, then fewer hard failures.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.agents.copy import quality
from app.agents.copy.dossier import build_brief, eligible_angles
from app.agents.copy.offer import OfferConfig
from app.agents.copy.prompts import (
    REVISION_TEMPLATE,
    copywriter_system_prompt,
    critic_system_prompt,
    strategist_system_prompt,
)
from app.agents.copy.types import (
    AITellType,
    CopyLead,
    CritiqueRecord,
    MessageResult,
    PSService,
    StrategyRecord,
)
from app.core.errors import SchemaValidationError
from app.llm.gateway import LLMGateway
from app.llm.types import Result, Task

DEFAULT_MAX_REVISIONS = 3


def _render_strategy(s: StrategyRecord) -> str:
    lines = [
        f"OPENER TYPE: {s.opener_type.value}",
        f"SUBJECT TO USE: {s.subject}",
        f"  (why it earns the open: {s.subject_rationale})",
        f"OPEN ON: {s.opener_observation}",
        f"  (source: {s.opener_source})",
        f"QUOTE VERDICT: {s.quote_verdict.value} -- {s.quote_reasoning}",
        f"PROBLEM TO NAME: {s.problem_statement}",
        f"ANGLE FOR THIS ROLE: {s.role_angle.value}",
        "VALUE POINTS (use one or two, no more):",
    ]
    lines += [f"  - {v}" for v in s.value_points]
    if s.competitor_used:
        lines.append(f"COMPETITOR TO NAME: {s.competitor_used}")
    lines += [
        f"THE ASK: {s.cta}",
        f"P.S.: {s.ps_service.value}"
        + (f" -- {s.ps_justification}" if s.ps_service != PSService.NONE else " (omit it)"),
        f"TONE TO MATCH: {s.tone}",
    ]
    if s.avoid:
        lines.append("AVOID ON THIS LEAD:")
        lines += [f"  - {a}" for a in s.avoid]
    return "\n".join(lines)


def _rank(score: int, scan: dict[str, Any]) -> tuple[int, int, int]:
    hard = scan["hard_failures"]
    return (0 if not hard else 1, -(score or 0), len(hard))


class CopywritingAgent:
    def __init__(
        self,
        *,
        gateway: LLMGateway,
        offer: OfferConfig,
        max_revisions: int = DEFAULT_MAX_REVISIONS,
    ) -> None:
        self._gateway = gateway
        self._offer = offer
        self._max_revisions = max_revisions

    # ------------------------------------------------------------ strategy ---

    async def _strategise(
        self, brief: str, *, today: date, campaign_id: UUID | None
    ) -> tuple[StrategyRecord, Decimal]:
        result = await self._gateway.complete(
            task=Task.STRATEGY,
            system=strategist_system_prompt(today=today, offer=self._offer),
            instructions=brief,
            schema=StrategyRecord,
            campaign_id=campaign_id,
        )
        if result.value is None:  # pragma: no cover - gateway guarantees this on success
            raise SchemaValidationError("strategy call returned no validated record")
        return result.value, result.cost_usd

    # --------------------------------------------------------------- write ---

    async def _write(
        self, brief: str, strategy: StrategyRecord, *, today: date, campaign_id: UUID | None
    ) -> tuple[str, str, str, Decimal]:
        instructions = (
            f"{brief}\n\n## Strategy to execute\n{_render_strategy(strategy)}\n\n"
            "Write the email now."
        )
        result: Result[None] = await self._gateway.complete(
            task=Task.COPY,
            system=copywriter_system_prompt(today=today, offer=self._offer),
            instructions=instructions,
            campaign_id=campaign_id,
        )
        subject, body = quality.split_email(result.text)
        return subject, body, result.model, result.cost_usd

    async def _revise(
        self,
        brief: str,
        strategy: StrategyRecord,
        subject: str,
        body: str,
        critique: CritiqueRecord,
        scan: dict[str, Any],
        *,
        today: date,
        campaign_id: UUID | None,
    ) -> tuple[str, str, str, Decimal]:
        problems = []
        for tell in critique.ai_tells:
            if tell.tell_type != AITellType.NONE and tell.quote:
                problems.append(f'- "{tell.quote}" ({tell.tell_type.value}) -> {tell.fix}')
        for fab in critique.fabrications:
            problems.append(f"- FABRICATION: {fab}")
        for h in scan.get("hard_failures", []):
            problems.append(f"- MECHANICAL: {h}")
        for s in scan.get("soft_warnings", []):
            problems.append(f"- minor: {s}")
        if not critique.swap_test_passed:
            problems.append(f"- SWAP TEST FAILED: {critique.swap_test_reasoning}")
        if critique.weakest_line:
            problems.append(f"- weakest line: {critique.weakest_line}")

        fixes = "\n".join(f"- {f}" for f in critique.required_fixes) or "- see above"

        # Length is the one instruction models reliably ignore when it sits
        # in a bullet list. State it first, in words, with the exact
        # arithmetic.
        words = scan.get("body_words", 0)
        length_order = ""
        if words > quality.BODY_WORD_MAX:
            length_order = (
                f"\n\nLENGTH IS THE PRIORITY FIX. Your draft is {words} words. "
                f"The hard limit is {quality.BODY_WORD_MAX}. Cut at least "
                f"{words - quality.BODY_WORD_MAX + 5} words. Delete whole sentences "
                "rather than trimming adjectives -- the weakest sentence is "
                "usually the one restating the problem a second time, or the "
                "second half of the product description. Do not shorten the "
                "opener and do not drop the ask.\n"
            )
        elif words and words < quality.BODY_WORD_MIN:
            length_order = (
                f"\n\nLENGTH IS THE PRIORITY FIX. Your draft is {words} words, "
                f"below the {quality.BODY_WORD_MIN} minimum. Add one concrete "
                "sentence of substance, not filler.\n"
            )

        instructions = (
            f"{brief}\n\n## Strategy to execute\n{_render_strategy(strategy)}\n\n"
            f"## Your previous draft\nSUBJECT: {subject}\n---\n{body}\n\n"
            + REVISION_TEMPLATE.format(
                score=critique.score,
                problems="\n".join(problems) or "- (none itemised)",
                fixes=fixes,
            )
            + length_order
        )
        result: Result[None] = await self._gateway.complete(
            task=Task.COPY,
            system=copywriter_system_prompt(today=today, offer=self._offer),
            instructions=instructions,
            campaign_id=campaign_id,
        )
        new_subject, new_body = quality.split_email(result.text)
        return new_subject, new_body, result.model, result.cost_usd

    # -------------------------------------------------------------- critic ---

    async def _critique(
        self,
        brief: str,
        strategy: StrategyRecord,
        subject: str,
        body: str,
        scan: dict[str, Any],
        *,
        campaign_id: UUID | None,
    ) -> tuple[CritiqueRecord, str, Decimal]:
        instructions = (
            f"## The lead and what is actually known about them\n{brief}\n\n"
            f"## The strategy the writer was given\n{_render_strategy(strategy)}\n\n"
            f"## {quality.render_scan(scan)}\n\n"
            f"## The draft to review\nSUBJECT: {subject}\n\n{body}\n\n"
            "Review it against all four tests and score it."
        )
        result = await self._gateway.complete(
            task=Task.CRITIQUE,
            system=critic_system_prompt(),
            instructions=instructions,
            schema=CritiqueRecord,
            campaign_id=campaign_id,
        )
        if result.value is None:  # pragma: no cover - gateway guarantees this on success
            raise SchemaValidationError("critique call returned no validated record")
        return result.value, result.model, result.cost_usd

    # ---------------------------------------------------------------- loop ---

    async def write_email(
        self,
        lead: CopyLead,
        angle: str,
        *,
        campaign_id: UUID | None = None,
        today: date | None = None,
    ) -> MessageResult:
        """One angle, one lead, one email. Fails loud (CLAUDE.md rule 2.1)
        if `angle` has no data on this lead -- `eligible_angles(lead)` is
        the caller's job to check first; a lead genuinely missing an
        angle's data is a normal skip at the caller, never a call made
        here in the first place.
        """
        if angle not in eligible_angles(lead):
            raise ValueError(f"lead {lead.company_name!r} has no eligible data for angle {angle!r}")
        today = today or datetime.now(UTC).date()
        brief = build_brief(lead, angle)
        total_cost = Decimal(0)

        strategy, cost = await self._strategise(brief, today=today, campaign_id=campaign_id)
        total_cost += cost

        subject, body, model_used, cost = await self._write(
            brief, strategy, today=today, campaign_id=campaign_id
        )
        total_cost += cost

        sender_name = self._offer.sender.name
        sender_company = self._offer.sender.company
        lead_name = lead.contact_name or ""
        allow_terms = [strategy.competitor_used] if strategy.competitor_used else []

        history: list[dict[str, Any]] = []
        critique: CritiqueRecord | None = None
        scan_result: dict[str, Any] = {}
        best: dict[str, Any] | None = None

        for attempt in range(self._max_revisions + 1):
            scan_result = quality.scan(
                subject,
                body,
                lead_name=lead_name,
                sender_name=sender_name,
                sender_company=sender_company,
                allow_terms=allow_terms,
                company=lead.company_name,
            )
            critique, critic_model, cost = await self._critique(
                brief, strategy, subject, body, scan_result, campaign_id=campaign_id
            )
            total_cost += cost
            model_used = critic_model

            score = critique.score or 0
            # A draft passes only if the model likes it AND nothing
            # mechanical is broken. The model does not get a vote on
            # whether an em dash is present.
            passed = (
                critique.passes
                and score >= quality.PASS_SCORE
                and scan_result["clean"]
                and not critique.fabrications
            )
            history.append(
                {
                    "attempt": attempt + 1,
                    "score": critique.score,
                    "passed": passed,
                    "hard_failures": scan_result["hard_failures"],
                    "soft_warnings": scan_result["soft_warnings"],
                    "fabrications": critique.fabrications,
                    "summary": critique.verdict_summary,
                }
            )

            # Only a draft with actual content can become the incumbent.
            if body.strip() and subject.strip():
                cand = {
                    "subject": subject,
                    "body": body,
                    "scan": scan_result,
                    "critique": critique,
                    "score": score,
                }
                if best is None or _rank(score, scan_result) < _rank(best["score"], best["scan"]):
                    best = cand

            if passed or attempt == self._max_revisions:
                break

            new_subject, new_body, model_used, cost = await self._revise(
                brief,
                strategy,
                subject,
                body,
                critique,
                scan_result,
                today=today,
                campaign_id=campaign_id,
            )
            total_cost += cost
            # An empty revision is a failed generation, not a valid draft.
            # Keep the previous text and let the next round try again.
            if new_body.strip() and new_subject.strip():
                subject, body = new_subject, new_body

        if best is not None:
            subject, body = best["subject"], best["body"]
            critique, scan_result = best["critique"], best["scan"]

        final_scan = quality.scan(
            subject,
            body,
            lead_name=lead_name,
            sender_name=sender_name,
            sender_company=sender_company,
            allow_terms=allow_terms,
            company=lead.company_name,
        )
        assert critique is not None  # the loop always runs at least once

        return MessageResult(
            angle=angle,
            subject=subject,
            body=body,
            passed=history[-1]["passed"] if history else False,
            score=critique.score,
            strategy=strategy.model_dump(mode="json"),
            critique=critique.model_dump(mode="json"),
            scan=final_scan,
            attempts=history,
            model=model_used,
            cost_usd=float(total_cost),
        )


__all__ = ["DEFAULT_MAX_REVISIONS", "CopywritingAgent"]
