"""Domain pattern learning — reverse-engineer a company's email format
from a single confirmed address.

    sarah.chen@northgate.com + "Sarah Chen"  ->  "{first}.{last}"

That one inference turns every future person at northgate.com from ~9
blind guesses into 1 targeted check. It is the highest-leverage function
in the acquisition pipeline and also the most dangerous: a *wrong*
learned pattern doesn't cost one bad guess, it poisons every future lead
at that domain, silently, forever.

So the rule is absolute: **when more than one pattern fits, learn
nothing.** Refusing to learn costs one domain's worth of blind guessing.
Learning wrong costs correctness on every lead there, and the failure is
invisible — the addresses look plausible and simply bounce.

Confirmation strength is graded, because the sources genuinely differ:

    REPLY         3   someone replied from it. Proof.
    VERIFICATION  2   an SMTP probe said VALID (Session 10)
    WEBSITE       1   found on the company's own site (Session 06)
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from app.resolvers.company.extract_email import ROLE_PREFIXES
from app.resolvers.email.patterns import (
    ALL_PATTERNS,
    first_name_variants,
    render_template,
    split_name,
    surname_variants,
)


class ConfirmationSource(enum.StrEnum):
    WEBSITE = "website"
    VERIFICATION = "verification"
    REPLY = "reply"


SOURCE_WEIGHTS: dict[ConfirmationSource, int] = {
    ConfirmationSource.WEBSITE: 1,
    ConfirmationSource.VERIFICATION: 2,
    ConfirmationSource.REPLY: 3,
}


@dataclass(frozen=True, slots=True)
class LearnedPattern:
    domain: str
    pattern: str
    weight: int
    source: ConfirmationSource


def matching_patterns(local_part: str, first: str, last: str) -> set[str]:
    """Every pattern template that could have produced this local part.

    Checked across all name variants, so "vanderberg" and "berg" both
    count as the surname when deciding whether "{last}" fits.
    """
    firsts = first_name_variants(first)
    lasts = surname_variants(last)
    if not firsts:
        return set()

    matches: set[str] = set()
    target = local_part.lower()
    for pattern in ALL_PATTERNS:
        for first_variant in firsts:
            for last_variant in lasts:
                if render_template(pattern.template, first_variant, last_variant) == target:
                    matches.add(pattern.template)
    return matches


def learn_from_confirmed(
    address: str,
    full_name: str,
    *,
    source: ConfirmationSource = ConfirmationSource.WEBSITE,
) -> LearnedPattern | None:
    """The learned format for this address's domain, or None when nothing
    can be learned safely.

    Returns None for: role accounts (info@ tells you nothing about how
    people are named), unparseable names, addresses that match no known
    pattern, and — the important one — addresses that match more than one.
    """
    local_part, _, domain = address.strip().lower().partition("@")
    if not local_part or not domain:
        return None

    # A role account is not a person's address. Learning "{first}" from
    # info@acme.com would teach the system that everyone at Acme is
    # reachable at their bare first name, which is simply false.
    if local_part in ROLE_PREFIXES:
        return None

    first, last = split_name(full_name)
    if not first:
        return None

    matches = matching_patterns(local_part, first, last)
    if len(matches) != 1:
        # 0 = an address we can't explain; >1 = genuine ambiguity, e.g. a
        # mononym where "{first}" and "{first}{last}" both render "cher".
        # Both cases learn nothing, deliberately.
        return None

    return LearnedPattern(
        domain=domain,
        pattern=matches.pop(),
        weight=SOURCE_WEIGHTS[source],
        source=source,
    )
