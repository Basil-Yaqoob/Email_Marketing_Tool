"""The four-part evidence bar, enforced in code — ported from
agent.py's `_normalise()`, extended with the parts CLAUDE.md rule 2.5
says belong in code rather than in a prompt.

All four or it's blank:

  SPECIFIC   - names something only this company did
  CURRENT    - within the freshness window
  SOURCED    - a URL, recorded (checked by the transcriber's schema too,
               demoted here independently — CLAUDE.md rule 2.2)
  BRIDGEABLE - left to the research prompt; not mechanically checkable

Then the swap test: put a different company's name in the sentence — does
it still read fine? The original applied this entirely as a model
instruction inside the research prompt. This module adds a mechanical,
code-level veto on top: does the finished sentence contain something a
generic sentence couldn't — a number/year, or a capitalised token beyond
the first word. Running that first, deterministically, is the project's
own rule (CLAUDE.md 2.5: deterministic checks are not delegated to the
model) applied to a bar the original left entirely to model judgement.
It can only make a FOUND hook stricter, never looser — a hook the model
already rejected stays rejected regardless.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from app.agents.hooks.types import HookRecord, HookResult
from app.db.models.enums import HookChannel, HookConfidenceLevel, HookNewsType, HookVerdict

DEFAULT_FRESHNESS_MONTHS = 12
DEFAULT_MAX_HOOK_WORDS = 25
_DAYS_PER_MONTH = 30.44  # matches the original's CUTOFF calculation exactly

_FULL_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
_YEAR_RE = re.compile(r"^(\d{4})$")

_DIGIT_RE = re.compile(r"\d")
_WORD_RE = re.compile(r"[A-Za-z']+")
_VERDICT_LINE_RE = re.compile(r"(?im)^VERDICT:\s*(found|none_found)\s*$")


def extract_report_verdict(report: str) -> HookVerdict | None:
    """The research report's own labelled VERDICT line, read mechanically
    — this is the enforcement behind "the transcriber cannot upgrade
    none_found to found". A prompt instruction alone ("you are a
    transcriber, not a researcher") is not a guarantee; this is: if the
    researcher's own report says none_found, the transcriber's claim of
    found is checked against it in code, not trusted (CLAUDE.md 2.5).

    Returns None when the report doesn't follow the labelled format at
    all, in which case there is nothing to cross-check against.
    """
    match = _VERDICT_LINE_RE.search(report)
    if match is None:
        return None
    return HookVerdict.FOUND if match.group(1).lower() == "found" else HookVerdict.NONE_FOUND


def freshness_cutoff(today: date, freshness_months: int) -> date:
    return today - timedelta(days=int(freshness_months * _DAYS_PER_MONTH))


def is_fresh(event_date: date, *, today: date, freshness_months: int) -> bool:
    return event_date >= freshness_cutoff(today, freshness_months)


def parse_hook_date(raw: str) -> date | None:
    """ "YYYY-MM-DD", "YYYY-MM" or "YYYY" -> a date. None for anything else
    — including empty, which is the correct value for a none_found record.
    """
    text = raw.strip()

    match = _FULL_DATE_RE.match(text)
    if match:
        try:
            return date(int(match[1]), int(match[2]), int(match[3]))
        except ValueError:
            return None

    match = _MONTH_RE.match(text)
    if match:
        try:
            return date(int(match[1]), int(match[2]), 1)
        except ValueError:
            return None

    match = _YEAR_RE.match(text)
    if match:
        return date(int(match[1]), 1, 1)

    return None


def specific_signal_present(hook: str) -> bool:
    """Mechanical half of the swap test: does the sentence contain
    something that could not apply to any company?

    A run of digits (a date, a count, a year) always counts. Otherwise, a
    capitalised word anywhere *after* the first — sentence-initial
    capitalisation is free and proves nothing — counts, since a real hook
    naming an award, a place, or a person surfaces one somewhere in the
    sentence. "They're growing fast" has neither and fails; "the MOJEH
    piece" has MOJEH at position 1 and passes.
    """
    if _DIGIT_RE.search(hook):
        return True

    words = _WORD_RE.findall(hook)
    return any(len(word) >= 2 and word[0].isupper() for word in words[1:])


def _normalise_hook_text(hook: str) -> str:
    # En dash and em dash both get flattened to a plain hyphen, ported
    # from the original's exact rule. Matched deliberately, not a typo.
    collapsed = " ".join(hook.split())
    return collapsed.replace("—", "-").replace("–", "-")  # noqa: RUF001


def _blank(reason: str, *, needs_review: bool = False, review_note: str = "") -> HookResult:
    return HookResult(
        status=HookVerdict.NONE_FOUND,
        hook_text=None,
        source_url=None,
        event_date=None,
        news_type=HookNewsType.NONE,
        channel=HookChannel.NONE,
        confidence=HookConfidenceLevel.LOW,
        swap_test_passed=False,
        needs_review=needs_review,
        notes=reason,
    )


def validate(
    record: HookRecord,
    *,
    today: date | None = None,
    freshness_months: int = DEFAULT_FRESHNESS_MONTHS,
    max_hook_words: int = DEFAULT_MAX_HOOK_WORDS,
) -> HookResult:
    today = today or date.today()

    # Provenance first, independent of what the transcriber claims: a
    # found hook missing either half of the pair is not a hook.
    if record.verdict == HookVerdict.FOUND and (
        not record.hook.strip() or not record.source_url.strip()
    ):
        return _blank(
            "model returned found without both a hook and a source URL",
            needs_review=True,
            review_note="demoted by validator",
        )

    if record.verdict == HookVerdict.NONE_FOUND:
        return _blank(
            record.rejection_reason.strip() or "no rejection reason given",
            needs_review=record.needs_review,
            review_note=record.review_note,
        )

    # From here: verdict is FOUND, hook and source_url are both present.
    hook_text = _normalise_hook_text(record.hook)
    source_url = record.source_url.strip()

    event_date = parse_hook_date(record.date)
    if event_date is None:
        return _blank(
            f"hook has no parseable date (got {record.date!r}); CURRENT cannot be verified",
            needs_review=True,
        )

    if not is_fresh(event_date, today=today, freshness_months=freshness_months):
        return _blank(
            f"event dated {event_date.isoformat()} is older than the "
            f"{freshness_months}-month freshness window"
        )

    if not specific_signal_present(hook_text):
        return _blank(
            "failed the mechanical swap test: no company-specific number, "
            "date or proper noun found in the sentence"
        )

    words = len(hook_text.split())
    needs_review = record.needs_review
    review_note = record.review_note.strip()
    if words > max_hook_words:
        needs_review = True
        over_length_note = f"hook exceeds {max_hook_words} words ({words})"
        review_note = (
            f"{review_note} {over_length_note}".strip() if review_note else over_length_note
        )

    return HookResult(
        status=HookVerdict.FOUND,
        hook_text=hook_text,
        source_url=source_url,
        event_date=event_date,
        news_type=record.news_type,
        channel=record.channel,
        confidence=record.confidence,
        swap_test_passed=True,
        needs_review=needs_review,
        notes=review_note,
    )
