"""Unit tests for email pattern generation and domain pattern learning —
pure logic, no I/O. The DomainPatternRepository's persistence side (get,
learn, weight-based conflict resolution) is tested against real Postgres
in tests/integration/test_repositories.py, alongside Session 02's
existing coverage of the same table.

Tests 3 and 13 are the ones that matter: test 3 is the economic claim
this whole session exists to make (measured, not asserted in prose), and
test 13 is the guard that keeps a wrong learned pattern from silently
poisoning every future lead at a domain.
"""

from __future__ import annotations

import itertools
from uuid import uuid4

import pytest

from app.resolvers.base import LeadContext
from app.resolvers.email.learning import ConfirmationSource, learn_from_confirmed
from app.resolvers.email.patterns import (
    CONF_BLIND_REST,
    CONF_BLIND_TOP,
    CONF_KNOWN_CONFIRMED,
    CONF_KNOWN_SINGLE,
    KnownPattern,
    first_name_variants,
    generate,
    split_name,
    surname_variants,
)
from app.resolvers.email.resolver import PatternEmailResolver


def _ctx(**overrides: object) -> LeadContext:
    defaults: dict[str, object] = {
        "company_id": uuid4(),
        "company_name": "Northgate Dental",
        "domain": "northgatedental.com",
        "website": "https://northgatedental.com",
        "country_code": "US",
        "person_name": "Sarah Chen",
    }
    defaults.update(overrides)
    return LeadContext(**defaults)  # type: ignore[arg-type]


class _FakeStore:
    def __init__(self, known: KnownPattern | None = None) -> None:
        self._known = known

    async def get(self, domain: str) -> KnownPattern | None:
        return self._known


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------


def test_generates_patterns_ranked_by_prior() -> None:
    candidates = generate("Sarah", "Chen", "acme.com")
    assert candidates[0].pattern == "{first}.{last}"
    assert candidates[0].value == "sarah.chen@acme.com"
    # Strictly non-increasing confidence as rank increases.
    confidences = [c.confidence for c in candidates]
    assert confidences == sorted(confidences, reverse=True)


def test_known_pattern_returns_single_candidate() -> None:
    known = KnownPattern(pattern="{first}.{last}", confirmed_count=2)
    candidates = generate("Sarah", "Chen", "acme.com", known=known)
    assert len(candidates) == 1
    assert candidates[0].value == "sarah.chen@acme.com"
    assert candidates[0].confidence == pytest.approx(CONF_KNOWN_CONFIRMED)


def test_measures_verification_volume_reduction() -> None:
    """The 9-to-1 claim, measured across a 100-lead fixture, not asserted
    in prose. >=8x fewer candidates need verification once every domain's
    format is known.
    """
    first_names = [
        "Sarah", "James", "Maria", "David", "Anna", "Chen", "Fatima", "Liam",
        "Priya", "Sean",
    ]  # fmt: skip
    last_names = [
        "Chen", "Smith", "Garcia", "Patel", "Muller", "OBrien", "Nowak",
        "Kowalski", "Rossi", "Ibrahim",
    ]  # fmt: skip
    leads = [
        (first, last, f"company{i}.example.test")
        for i, (first, last) in enumerate(itertools.product(first_names, last_names))
    ]
    assert len(leads) == 100

    unknown_total = sum(len(generate(first, last, domain)) for first, last, domain in leads)
    known_total = sum(
        len(
            generate(
                first,
                last,
                domain,
                known=KnownPattern("{first}.{last}", confirmed_count=2),
            )
        )
        for first, last, domain in leads
    )

    assert known_total == 100  # exactly one candidate per lead, by construction
    ratio = unknown_total / known_total
    assert ratio >= 8.0, f"volume reduction {ratio:.1f}x is below the 8x floor"


def test_confidence_never_reaches_send_threshold() -> None:
    """Generated != verified. Every confidence this module can produce,
    known pattern or blind, stays below the 0.85 send threshold
    (app/core/config.py:confidence_threshold's default).
    """
    send_threshold = 0.85
    assert send_threshold > CONF_KNOWN_CONFIRMED
    assert send_threshold > CONF_KNOWN_SINGLE
    assert send_threshold > CONF_BLIND_TOP
    assert send_threshold > CONF_BLIND_REST

    all_confidences = [c.confidence for c in generate("Sarah", "Chen", "acme.com")]
    all_confidences += [
        c.confidence
        for c in generate("Sarah", "Chen", "acme.com", known=KnownPattern("{first}.{last}", 5))
    ]
    assert all(c < send_threshold for c in all_confidences)


def test_healthcare_extras_not_applied_to_other_industries() -> None:
    generic = {c.pattern for c in generate("Sarah", "Chen", "acme.com", limit=99)}
    dental = {c.pattern for c in generate("Sarah", "Chen", "acme.com", industry="dental", limit=99)}
    assert "dr{last}" not in generic
    assert "dr{last}" in dental
    assert "dr.{last}" in dental


def test_single_word_name_handled() -> None:
    """A mononym must not crash -- templates needing a surname simply
    don't render, rather than producing a malformed address.
    """
    candidates = generate("Cher", "", "acme.com")
    assert candidates
    assert all("@acme.com" in c.value for c in candidates)
    assert all(not c.value.startswith("@") for c in candidates)


def test_blind_guess_count_is_capped() -> None:
    candidates = generate("Sarah", "Chen", "acme.com")
    from app.resolvers.email.patterns import MAX_BLIND_GUESSES

    assert len(candidates) <= MAX_BLIND_GUESSES


def test_generate_returns_nothing_for_an_empty_first_name() -> None:
    assert generate("", "Smith", "acme.com") == []


def test_known_pattern_unrenderable_for_this_name_returns_nothing() -> None:
    """The domain's known format needs a surname this person doesn't
    have -- must not fall back to blind guessing, which would throw away
    the exact saving a known pattern exists to produce.
    """
    known = KnownPattern(pattern="{first}.{last}", confirmed_count=2)
    assert generate("Sarah", "", "acme.com", known=known) == []


def test_render_template_rejects_doubled_separators() -> None:
    from app.resolvers.email.patterns import render_template

    assert render_template("{first}__{last}", "a", "b") is None
    assert render_template("{first}..{last}", "a", "b") is None


def test_split_name_on_only_honorifics_returns_empty() -> None:
    assert split_name("Dr.") == ("", "")


# --------------------------------------------------------------------------
# Name normalisation
# --------------------------------------------------------------------------


def test_normalises_accents() -> None:
    first, last = first_name_variants("José")[0], surname_variants("García")[0]
    assert first == "jose"
    assert last == "garcia"


def test_german_umlaut_expansion() -> None:
    """Müller -> mueller, not muller -- the convention companies actually
    use.
    """
    variants = surname_variants("Müller")
    assert "mueller" in variants
    assert "muller" not in variants


def test_strips_apostrophes() -> None:
    variants = surname_variants("O'Brien")
    assert "obrien" in variants
    # A typographic (curly) apostrophe, not a typo -- copy-pasted names
    # use it as often as a straight one and must normalise the same way.
    curly_variants = surname_variants("O’Brien")  # noqa: RUF001
    assert "obrien" in curly_variants


def test_strips_credentials_and_honorifics() -> None:
    first, last = split_name("Dr. Jane Smith, DDS")
    assert first == "Jane"
    assert last == "Smith"

    first2, last2 = split_name("Prof. John Doe MD")
    assert first2 == "John"
    assert last2 == "Doe"


def test_multi_part_surname_variants() -> None:
    first, last = split_name("Johannes Willem van der Berg")
    assert first == "Johannes"
    assert last == "van der Berg"

    variants = surname_variants(last)
    assert "vanderberg" in variants
    assert "berg" in variants


def test_hyphenated_surname_both_forms() -> None:
    variants = surname_variants("Smith-Jones")
    assert "smithjones" in variants
    assert "smith-jones" in variants


# --------------------------------------------------------------------------
# Learning
# --------------------------------------------------------------------------


def test_learns_first_dot_last() -> None:
    learned = learn_from_confirmed("sarah.chen@northgate.com", "Sarah Chen")
    assert learned is not None
    assert learned.pattern == "{first}.{last}"
    assert learned.domain == "northgate.com"


def test_learns_f_last() -> None:
    learned = learn_from_confirmed("schen@northgate.com", "Sarah Chen")
    assert learned is not None
    assert learned.pattern == "{f}{last}"


def test_learns_first_only() -> None:
    learned = learn_from_confirmed("sarah@northgate.com", "Sarah Chen")
    assert learned is not None
    assert learned.pattern == "{first}"


def test_ambiguous_local_part_learns_nothing() -> None:
    """The poisoning guard: a mononym's address matches both {first} and
    {first}{last} (since last is empty) -- genuinely ambiguous, and must
    learn nothing rather than guess.
    """
    assert learn_from_confirmed("cher@acme.com", "Cher") is None


def test_unmatched_local_part_learns_nothing() -> None:
    assert learn_from_confirmed("random123@acme.com", "Sarah Chen") is None


def test_learn_from_confirmed_rejects_a_malformed_address() -> None:
    assert learn_from_confirmed("not-an-email-at-all", "Sarah Chen") is None
    assert learn_from_confirmed("", "Sarah Chen") is None


def test_matching_patterns_empty_for_a_blank_first_name() -> None:
    from app.resolvers.email.learning import matching_patterns

    assert matching_patterns("jsmith", "", "Smith") == set()


def test_role_account_never_teaches_a_pattern() -> None:
    """info@ tells you nothing about how real people are named at this
    domain -- learning from it would poison every future person.
    """
    assert learn_from_confirmed("info@acme.com", "Sarah Chen") is None
    assert learn_from_confirmed("support@acme.com", "Sarah Chen") is None


def test_reply_source_carries_highest_weight() -> None:
    from app.resolvers.email.learning import SOURCE_WEIGHTS

    assert (
        SOURCE_WEIGHTS[ConfirmationSource.REPLY]
        > SOURCE_WEIGHTS[ConfirmationSource.VERIFICATION]
        > SOURCE_WEIGHTS[ConfirmationSource.WEBSITE]
    )


def test_learn_from_confirmed_defaults_to_website_source() -> None:
    learned = learn_from_confirmed("sarah.chen@northgate.com", "Sarah Chen")
    assert learned is not None
    assert learned.source == ConfirmationSource.WEBSITE


# --------------------------------------------------------------------------
# PatternEmailResolver
# --------------------------------------------------------------------------


async def test_resolver_uses_known_pattern_from_store() -> None:
    store = _FakeStore(known=KnownPattern("{first}.{last}", confirmed_count=3))
    resolver = PatternEmailResolver(store=store)

    candidates = await resolver.resolve(_ctx())

    assert len(candidates) == 1
    assert candidates[0].value == "sarah.chen@northgatedental.com"
    assert candidates[0].confidence == pytest.approx(CONF_KNOWN_CONFIRMED)
    assert candidates[0].extra["verified"] is False
    assert candidates[0].extra["from_known_pattern"] is True


async def test_resolver_falls_back_to_blind_guessing() -> None:
    resolver = PatternEmailResolver(store=_FakeStore(known=None))
    candidates = await resolver.resolve(_ctx())
    assert len(candidates) > 1
    assert all(c.extra["from_known_pattern"] is False for c in candidates)


async def test_resolver_applicable_with_domain_and_person() -> None:
    resolver = PatternEmailResolver(store=_FakeStore())
    assert await resolver.applicable(_ctx()) is True


async def test_resolve_returns_empty_when_called_directly_without_domain() -> None:
    """resolve()'s own guard, independent of applicable() -- exercised by
    calling resolve() directly rather than through the executor, which
    always checks applicable() first.
    """
    resolver = PatternEmailResolver(store=_FakeStore())
    assert await resolver.resolve(_ctx(domain=None)) == []


async def test_resolver_not_applicable_without_domain() -> None:
    resolver = PatternEmailResolver(store=_FakeStore())
    ctx = _ctx(domain=None)
    assert await resolver.applicable(ctx) is False


async def test_resolver_not_applicable_without_person_name() -> None:
    resolver = PatternEmailResolver(store=_FakeStore())
    ctx = _ctx(person_name=None)
    assert await resolver.applicable(ctx) is False
