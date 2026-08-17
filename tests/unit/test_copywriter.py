"""Unit tests for the copywriting pipeline, ported from `email_writer`.
Network is never touched: the LLM is a scripted `Provider` double wired
into a real `LLMGateway`, exactly as `tests/unit/test_hook_mining.py` does
for Session 12.

Tests 5, 6, 11 and 24 are the load-bearing ones, per the plan:

  test_the_four_original_bad_subjects_fail_the_gate — real regression
      suite; three of the four are sourced verbatim from the prototype's
      README/prompts.py, the fourth is a constructed same-class instance
      (a colon + Title Case + banned-vocabulary subject) since the
      prototype's own docs claim "four" but only record three literal
      examples — see that test's docstring.
  test_the_three_good_subjects_pass — the same regression suite's other half.
  test_model_approval_cannot_override_mechanical_failure — the critic
      scores 10 and a mechanical em dash still rejects the draft.
  test_blank_proof_points_never_invented — an empty offer forbids
      invented proof both in the rendered prompt and in the pass/fail gate.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import pytest
from pydantic import BaseModel

from app.agents.copy.agents import DEFAULT_MAX_REVISIONS, CopywritingAgent
from app.agents.copy.dossier import build_brief, eligible_angles
from app.agents.copy.offer import OfferConfig, ProductOffer, ProofPoint, Sender
from app.agents.copy.quality import (
    names_company,
    render_scan,
    sanitize,
    scan,
    split_email,
)
from app.agents.copy.quote_classification import (
    QUOTE_PLAYBOOK,
    ROLE_ANGLE,
    QuoteClass,
    classify_quote,
    derive_wedge,
)
from app.agents.copy.types import CompetitorInfo, CopyLead
from app.db.models.enums import RoleClass
from app.llm.cost import InMemoryCostTracker
from app.llm.gateway import LLMGateway
from app.llm.prices import ModelPrice, PriceTable
from app.llm.types import FinishReason, Message, ProviderResponse, Tool, Usage


async def _no_sleep(delay: float) -> None:
    return None


# --------------------------------------------------------------------------
# quality.py — the deterministic scanner
# --------------------------------------------------------------------------


def test_em_dash_detected_mechanically() -> None:
    body = "This works great — no question about it, honestly plain text here."
    result = scan("a fine subject", body)
    assert not result["clean"]
    assert any("dash" in f for f in result["hard_failures"])


@pytest.mark.parametrize(
    "phrase",
    ["I hope this email finds you well", "My name is", "I'm reaching out"],
)
def test_banned_phrases_detected(phrase: str) -> None:
    result = scan("a fine subject", f"{phrase} and I wanted to say hello to you today.")
    assert not result["clean"]
    assert any("banned phrase" in f for f in result["hard_failures"])


@pytest.mark.parametrize(
    ("n_words", "should_fail"),
    [(54, True), (55, False), (135, False), (136, True)],
)
def test_word_count_bounds_enforced(n_words: int, should_fail: bool) -> None:
    body = " ".join(["word"] * n_words)
    result = scan("a fine subject", body)
    has_length_failure = any("body is" in f for f in result["hard_failures"])
    assert has_length_failure is should_fail


def test_missing_contractions_flagged() -> None:
    body = " ".join(["word"] * 70)
    result = scan("a fine subject", body)
    assert "no contractions anywhere; reads formal and machine-written" in result["soft_warnings"]
    # A soft warning does not fail the gate on its own.
    assert result["clean"]


BAD_SUBJECTS = [
    "your award and your phone",
    "your DIFC expansion and after-hours calls",
    "your weekend voicemail problem",
    # Constructed same-class instance -- see module docstring: the source
    # material only records three literal historical examples.
    "Introducing: Your New AI Receptionist",
]

GOOD_SUBJECTS = ["the MOJEH piece", "Laser One", "your saturday callers"]


@pytest.mark.parametrize("subject", BAD_SUBJECTS)
def test_the_four_original_bad_subjects_fail_the_gate(subject: str) -> None:
    body = " ".join(["word"] * 70)
    result = scan(subject, body)
    assert not result["clean"], f"{subject!r} should have failed the gate"


@pytest.mark.parametrize("subject", GOOD_SUBJECTS)
def test_the_three_good_subjects_pass(subject: str) -> None:
    body = " ".join(["word"] * 70)
    result = scan(subject, body)
    assert result["clean"], f"{subject!r} should have passed: {result['hard_failures']}"


def test_subject_over_five_words_rejected() -> None:
    result = scan("this subject has exactly six words", " ".join(["word"] * 70))
    assert any("subject is" in f and "words" in f for f in result["hard_failures"])


def test_title_case_subject_rejected() -> None:
    result = scan("Your Practice And The Phone", " ".join(["word"] * 70))
    assert any("Title Case" in f for f in result["hard_failures"])


def test_subject_containing_benefit_claim_rejected() -> None:
    result = scan("boost your revenue", " ".join(["word"] * 70))
    assert any("reveals the pitch" in f for f in result["hard_failures"])


def test_your_x_and_your_y_shape_rejected() -> None:
    result = scan("your team and your patients", " ".join(["word"] * 70))
    assert any("template shape" in f for f in result["hard_failures"])


def test_split_email_extracts_subject_and_sanitizes() -> None:
    raw = "SUBJECT: the MOJEH piece\n---\nHi there — this is the body.\n"
    subject, body = split_email(raw)
    assert subject == "the MOJEH piece"
    assert "—" not in body
    assert "-" in body


def test_sanitize_flattens_smart_punctuation() -> None:
    text = sanitize("“quoted” — and ‘this’…")  # noqa: RUF001
    assert "“" not in text
    assert "—" not in text
    assert "’" not in text  # noqa: RUF001


def test_names_company_accepts_shortened_form() -> None:
    body = "How about an AI receptionist for Year One Wellness that answers every call?"
    registered_name = "Year One Wellness: Pediatric Physical Therapy & Occupational Therapy"
    assert names_company(body, registered_name)


def test_names_company_rejects_generic_only_mention() -> None:
    body = "How about an AI receptionist for your clinic that answers every call?"
    assert not names_company(body, "Riverside Dental")


def test_render_scan_labels_hard_and_soft() -> None:
    result = scan("Your Team And Your Patients", " ".join(["word"] * 30))
    text = render_scan(result)
    assert "HARD:" in text
    assert "soft:" in text or "clean" not in text


# --------------------------------------------------------------------------
# quote_classification.py
# --------------------------------------------------------------------------


def test_inverted_praise_flips_the_framing() -> None:
    quote = "I want to thank the girls at reception, they were very kind."
    verdict = classify_quote(quote)
    assert verdict == QuoteClass.PRAISE
    assert "COMPLIMENT" in QUOTE_PLAYBOOK[verdict]
    assert "Invert it" in QUOTE_PLAYBOOK[verdict]


def test_scheduling_friction_opens_on_scheduling() -> None:
    quote = "They rescheduled my appointment twice and I had to wait forever."
    verdict = classify_quote(quote)
    assert verdict == QuoteClass.SCHEDULING_COMPLAINT
    assert "scheduling friction" in QUOTE_PLAYBOOK[verdict]


def test_discarded_unclear_falls_back_to_observable_fact() -> None:
    quote = "It was fine I guess."
    verdict = classify_quote(quote)
    assert verdict == QuoteClass.UNCLEAR
    assert "observable operational fact" in QUOTE_PLAYBOOK[verdict]


def test_phone_complaint_is_the_strongest_case() -> None:
    quote = "No one answered the phone and they never called back."
    assert classify_quote(quote) == QuoteClass.PHONE_COMPLAINT


def test_owner_role_gets_revenue_angle() -> None:
    assert "evenue" in ROLE_ANGLE[RoleClass.OWNER]


def test_marketing_role_gets_softened_passalong_ask() -> None:
    assert "pass-along ask" in ROLE_ANGLE[RoleClass.MARKETING]


def test_derive_wedge_prefers_explicit_value() -> None:
    lead = _lead(primary_wedge="after_hours", missed_call_evidence="no one answered ever")
    assert derive_wedge(lead) == "after_hours"


def test_derive_wedge_infers_missed_calls_from_phone_complaint() -> None:
    lead = _lead(primary_wedge=None, missed_call_evidence="no one answered when I called")
    assert derive_wedge(lead) == "missed_calls"


# --------------------------------------------------------------------------
# dossier.py — eligibility and brief construction
# --------------------------------------------------------------------------


def _lead(**overrides: object) -> CopyLead:
    defaults: dict[str, object] = {"company_name": "Riverside Dental"}
    defaults.update(overrides)
    return CopyLead(**defaults)  # type: ignore[arg-type]


def test_lead_without_angle_data_is_skipped_not_failed() -> None:
    lead = _lead()  # no missed-call evidence, no hook, no competitors
    assert eligible_angles(lead) == []

    lead = _lead(missed_call_evidence="no one answered when I called")
    assert eligible_angles(lead) == ["missed_call"]


def test_claim_without_provenance_rejected() -> None:
    """A hook with text but no source URL never becomes an eligible angle —
    the same provenance bar Session 12's validator enforces before a hook
    is persisted (CLAUDE.md rule 2.2), applied again here independently.
    """
    lead = _lead(hook_text="opened a new branch downtown", hook_source_url=None)
    assert "news_hook" not in eligible_angles(lead)

    lead = _lead(
        hook_text="opened a new branch downtown", hook_source_url="https://example.test/news"
    )
    assert "news_hook" in eligible_angles(lead)


def test_competitor_angle_names_the_rival() -> None:
    rival = CompetitorInfo(name="Laser One", threat=9.0, rating="4.8", why="fast booking")
    lead = _lead(competitors=(rival,))
    brief = build_brief(lead, "competitor")
    # The rival is named as an actual list entry, not just present as
    # illustrative guidance text (which the brief also legitimately
    # contains -- see dossier.build_brief's "worth nothing" line).
    assert "- Laser One | " in brief
    assert "fast booking" in brief


def test_angles_argued_independently() -> None:
    lead = _lead(
        missed_call_evidence="callers say no one ever answers the phone",
        competitors=(CompetitorInfo(name="Laser One", threat=9.0),),
    )
    competitor_brief = build_brief(lead, "competitor")
    assert "no one ever answers the phone" not in competitor_brief

    missed_call_brief = build_brief(lead, "missed_call")
    assert "Laser One" not in missed_call_brief


# --------------------------------------------------------------------------
# offer.py
# --------------------------------------------------------------------------


def _offer(*, proof_points: tuple[ProofPoint, ...] = ()) -> OfferConfig:
    return OfferConfig(
        sender=Sender(name="Alex Rivera", company="Ferrylane", calendar_or_reply="reply"),
        primary=ProductOffer(
            name="AI receptionist",
            one_liner="answers the phone and books straight into the calendar",
            mechanics=("answers every call on the first or second ring",),
        ),
        proof_points=proof_points,
    )


def test_asks_for_reply_not_calendar_booking() -> None:
    assert "ask for a reply, never a calendar link" in _offer().render_sender()


def test_sender_ready_flags_missing_fields() -> None:
    offer = OfferConfig(sender=Sender(), primary=ProductOffer(name="x", one_liner="y"))
    ready, missing = offer.sender_ready()
    assert not ready
    assert set(missing) == {"name", "company"}


def test_blank_proof_points_never_invented_in_prompt() -> None:
    text = _offer(proof_points=()).render_offer()
    assert "PROOF: none supplied" in text
    assert "You may NOT cite results, numbers, percentages" in text


# --------------------------------------------------------------------------
# Agent — real LLMGateway wired to a scripted Provider double
# --------------------------------------------------------------------------


@dataclass
class ScriptedProvider:
    responses: list[object]
    name: str = "fake"
    calls: list[dict[str, object]] = field(default_factory=list)

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        schema: type[BaseModel] | None = None,
        tools: Sequence[Tool] | None = None,
    ) -> ProviderResponse:
        self.calls.append({"model": model, "messages": list(messages), "schema": schema})
        if not self.responses:
            raise AssertionError("ScriptedProvider ran out of scripted responses")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, ProviderResponse)
        return item


def _text_response(text: str) -> ProviderResponse:
    return ProviderResponse(
        text=text, model="fake-model", usage=Usage(50, 30), finish_reason=FinishReason.STOP
    )


def _strategy_response(**overrides: object) -> ProviderResponse:
    defaults: dict[str, object] = {
        "opener_type": "observable_wedge",
        "subject": "your busy afternoons",
        "opener_observation": "front desk gets busy some afternoons",
        "opener_source": "review themes",
        "quote_verdict": "no_quote",
        "quote_reasoning": "",
        "competitor_used": "",
        "subject_rationale": "names the specific pattern without giving away the pitch",
        "problem_statement": "missed afternoon calls likely go to the next practice",
        "role_angle": "revenue",
        "value_points": ["answers every call on the first or second ring"],
        "cta": "ask if it's worth a look",
        "ps_service": "none",
        "ps_justification": "",
        "tone": "plain, peer to peer",
        "avoid": [],
        "confidence": "medium",
    }
    defaults.update(overrides)
    return _text_response(json.dumps(defaults))


def _critique_response(**overrides: object) -> ProviderResponse:
    defaults: dict[str, object] = {
        "score": 9,
        "passes": True,
        "ai_tells": [],
        "subject_verdict": "good",
        "subject_fix": "",
        "swap_test_passed": True,
        "swap_test_reasoning": "opener is specific to this lead",
        "fabrications": [],
        "reply_likelihood": "medium",
        "weakest_line": "",
        "required_fixes": [],
        "verdict_summary": "solid draft",
    }
    defaults.update(overrides)
    return _text_response(json.dumps(defaults))


# Newlines inside a paragraph don't change word count (scan() splits body
# on any whitespace), so these are wrapped for line length without
# changing what the mechanical scanner measures.
_CLEAN_DRAFT = """\
SUBJECT: your busy afternoons
---
Jane,

I saw Riverside Dental's new patient reviews mention how busy the front desk
gets some afternoons.

That kind of call volume usually means a few callers hit voicemail before
someone picks up, and each one is a booking that might just call the next
practice instead.

How about an AI receptionist for Riverside Dental that answers every call on
the first or second ring, books straight into the calendar you already use,
and texts back the moment a call is missed?

Worth a quick look at how it would work for you?

Alex Rivera
Ferrylane
"""

# Uses "!" rather than an em dash for the mechanical violation: split_email
# sanitizes the draft (stripping em/en dashes and emoji) before scan() ever
# sees it, exactly as the original's write()/revise() do -- an em dash
# embedded in a scripted model response would be silently cleaned before
# reaching the scanner. "!" survives sanitize() untouched and scan() still
# flags it as a hard failure, so it exercises the same "model gets no vote"
# guarantee without fighting the pipeline's own sanitization step.
_LOUD_DRAFT = """\
SUBJECT: your busy afternoons
---
Jane,

I saw Riverside Dental's new patient reviews mention how busy the front desk
gets some afternoons! That really stood out to me.

That kind of call volume usually means a few callers hit voicemail before
someone picks up, and each one is a booking that might just call the next
practice instead.

How about an AI receptionist for Riverside Dental that answers every call on
the first or second ring, books straight into the calendar you already use,
and texts back the moment a call is missed?

Worth a quick look at how it would work for you?

Alex Rivera
Ferrylane
"""

_FABRICATED_DRAFT = """\
SUBJECT: your busy afternoons
---
Jane,

I saw Riverside Dental's new patient reviews mention how busy the front desk
gets some afternoons.

That kind of call volume usually means a few callers hit voicemail before
someone picks up, and each one is a booking that might just call the next
practice instead. Practices like yours typically see 40% more bookings
within a month.

How about an AI receptionist for Riverside Dental that answers every call on
the first or second ring, books straight into the calendar you already use,
and texts back the moment a call is missed?

Worth a quick look at how it would work for you?

Alex Rivera
Ferrylane
"""


def _agent(
    provider: ScriptedProvider,
    *,
    offer: OfferConfig | None = None,
    max_revisions: int = DEFAULT_MAX_REVISIONS,
) -> CopywritingAgent:
    gateway = LLMGateway(
        providers={"fake": provider},
        prices=PriceTable(
            {"fake-model": ModelPrice(Decimal("0.1"), Decimal("0.4"))}, fetched_at=date.today()
        ),
        tracker=InMemoryCostTracker(),
        sleep=_no_sleep,
    )
    return CopywritingAgent(gateway=gateway, offer=offer or _offer(), max_revisions=max_revisions)


async def test_model_approval_cannot_override_mechanical_failure() -> None:
    provider = ScriptedProvider(
        responses=[
            _strategy_response(),
            _text_response(_LOUD_DRAFT),
            _critique_response(score=10, passes=True),
        ]
    )
    agent = _agent(provider, max_revisions=0)
    lead = _lead(contact_name="Jane", missed_call_evidence="no one answered when I called")

    result = await agent.write_email(lead, "missed_call")

    assert not result.scan["clean"]
    assert any("exclamation" in f for f in result.scan["hard_failures"])
    assert result.critique["score"] == 10
    assert result.critique["passes"] is True
    msg = "a mechanical failure must reject the draft regardless of the model's score"
    assert result.passed is False, msg


async def test_revision_prompt_carries_critique_and_length_feedback() -> None:
    """The revise() call's own instructions -- not just the pass/fail
    verdict -- carry the critic's ai_tells, fabrications, swap-test result,
    weakest line, and (when the draft ran long) the length-cut order.
    """
    overlong_draft = "SUBJECT: your busy afternoons\n---\n" + " ".join(["word"] * 140)
    provider = ScriptedProvider(
        responses=[
            _strategy_response(),
            _text_response(overlong_draft),
            _critique_response(
                score=5,
                passes=False,
                ai_tells=[{"quote": "leverage", "tell_type": "vocabulary", "fix": "use"}],
                fabrications=["invented a 40% claim"],
                swap_test_passed=False,
                swap_test_reasoning="reads the same for any clinic",
                weakest_line="the second paragraph restates the first",
            ),
            _text_response(_CLEAN_DRAFT),
            _critique_response(),
        ]
    )
    agent = _agent(provider, max_revisions=1)
    lead = _lead(contact_name="Jane", missed_call_evidence="no one answered when I called")

    await agent.write_email(lead, "missed_call")

    revise_call = provider.calls[3]
    instructions = next(m.content for m in revise_call["messages"] if m.role.value == "user")
    assert "leverage" in instructions
    assert "vocabulary" in instructions
    assert "FABRICATION: invented a 40% claim" in instructions
    assert "SWAP TEST FAILED: reads the same for any clinic" in instructions
    assert "weakest line: the second paragraph restates the first" in instructions
    assert "LENGTH IS THE PRIORITY FIX" in instructions
    assert "140 words" in instructions


async def test_mechanical_findings_passed_to_critic_as_evidence() -> None:
    provider = ScriptedProvider(
        responses=[
            _strategy_response(),
            _text_response(_LOUD_DRAFT),
            _critique_response(),
        ]
    )
    agent = _agent(provider, max_revisions=0)
    lead = _lead(contact_name="Jane", missed_call_evidence="no one answered when I called")

    await agent.write_email(lead, "missed_call")

    critique_call = provider.calls[2]
    messages = critique_call["messages"]
    instructions = next(m.content for m in messages if m.role.value == "user")
    assert "Mechanical scan findings" in instructions
    assert "HARD:" in instructions


async def test_revision_loop_capped_at_three() -> None:
    responses: list[object] = [_strategy_response(), _text_response(_CLEAN_DRAFT)]
    for _ in range(3):
        responses.append(_critique_response(score=5, passes=False))
        responses.append(_text_response(_CLEAN_DRAFT))
    responses.append(_critique_response(score=5, passes=False))  # the final, non-revised attempt

    provider = ScriptedProvider(responses=responses)
    agent = _agent(provider)  # default max_revisions = 3
    lead = _lead(contact_name="Jane", missed_call_evidence="no one answered when I called")

    result = await agent.write_email(lead, "missed_call")

    assert len(result.attempts) == 4  # one initial attempt plus exactly three revisions
    assert provider.responses == []  # every scripted response was consumed, none left over
    assert result.passed is False


async def test_strategist_returns_strict_json() -> None:
    provider = ScriptedProvider(
        responses=[
            _strategy_response(opener_type="review_quote", role_angle="workload"),
            _text_response(_CLEAN_DRAFT),
            _critique_response(),
        ]
    )
    agent = _agent(provider, max_revisions=0)
    lead = _lead(contact_name="Jane", missed_call_evidence="no one answered when I called")

    result = await agent.write_email(lead, "missed_call")

    assert result.strategy["opener_type"] == "review_quote"
    assert result.strategy["role_angle"] == "workload"


async def test_quote_verdict_recorded_per_message() -> None:
    provider = ScriptedProvider(
        responses=[
            _strategy_response(quote_verdict="inverted_praise"),
            _text_response(_CLEAN_DRAFT),
            _critique_response(),
        ]
    )
    agent = _agent(provider, max_revisions=0)
    lead = _lead(contact_name="Jane", missed_call_evidence="the staff were lovely, thank you")

    result = await agent.write_email(lead, "missed_call")

    assert result.strategy["quote_verdict"] == "inverted_praise"


async def test_blank_proof_points_never_invented() -> None:
    provider = ScriptedProvider(
        responses=[
            _strategy_response(),
            _text_response(_FABRICATED_DRAFT),
            _critique_response(
                score=9,
                passes=True,
                fabrications=["claims '40% more bookings', which is not in the proof points"],
            ),
        ]
    )
    agent = _agent(provider, offer=_offer(proof_points=()), max_revisions=0)
    lead = _lead(contact_name="Jane", missed_call_evidence="no one answered when I called")

    result = await agent.write_email(lead, "missed_call")

    assert result.critique["fabrications"]
    msg = "a fabrication must reject the draft even if the critic 'passes' it"
    assert result.passed is False, msg


async def test_write_email_raises_on_ineligible_angle() -> None:
    provider = ScriptedProvider(responses=[])
    agent = _agent(provider)
    lead = _lead()  # no data for any angle

    with pytest.raises(ValueError, match="no eligible data"):
        await agent.write_email(lead, "missed_call")
