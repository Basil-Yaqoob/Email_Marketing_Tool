"""PatternEmailResolver — generated candidate addresses as a FREE tier
resolver.

Free because generating an address costs nothing; the *verification* is
what costs money (Session 10). That split is the whole point: this
resolver's job is to hand verification as few hypotheses as possible,
one when the domain's format is known and a ranked handful when it isn't.

Every candidate this returns is below the send threshold by construction.
An address here has never been checked against a mail server.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from app.resolvers.base import BaseResolver, Candidate, LeadContext, Tier
from app.resolvers.email.patterns import KnownPattern, generate, split_name


class PatternStore(Protocol):
    """Read side of domain_patterns, narrowed to what the resolver needs.

    A Protocol rather than the repository itself so this resolver stays
    unit-testable with a fake and no database, the same way the waterfall
    tests use fake resolvers (Session 03).
    """

    async def get(self, domain: str) -> KnownPattern | None: ...


class PatternEmailResolver(BaseResolver):
    name = "email_pattern"
    field = "email"
    tier = Tier.FREE
    cost_per_call = Decimal("0")
    jurisdictions: frozenset[str] | None = None  # worldwide

    def __init__(self, *, store: PatternStore) -> None:
        self._store = store

    async def applicable(self, ctx: LeadContext) -> bool:
        """Needs both a domain to build addresses at and a person to build
        them for. Missing either is a SKIP, not an error — plenty of leads
        legitimately reach this resolver with no name resolved yet.
        """
        if not ctx.domain or not ctx.person_name:
            return False
        return await super().applicable(ctx)

    async def resolve(self, ctx: LeadContext) -> list[Candidate]:
        # Repeats applicable()'s guard rather than assuming it ran: the
        # executor always calls applicable() first, but resolve() is also
        # called directly (tests, and any future orchestration), and a
        # missing domain here should be an empty result, not a crash.
        domain, person_name = ctx.domain, ctx.person_name
        if not domain or not person_name:
            return []

        first, last = split_name(person_name)
        known = await self._store.get(domain)

        candidates = generate(
            first,
            last,
            domain,
            known=known,
            industry=ctx.known_facts.get("industry"),
        )

        return [
            Candidate(
                value=candidate.value,
                confidence=candidate.confidence,
                source=self.name,
                extra={
                    "pattern": candidate.pattern,
                    "rank": candidate.rank,
                    "from_known_pattern": known is not None,
                    # Explicit, because it is the rule that keeps the
                    # prototype's failure from recurring: nothing here has
                    # been checked against a mail server yet.
                    "verified": False,
                },
            )
            for candidate in candidates
        ]
