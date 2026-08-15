"""ResolverRegistry — explicit registration, no import-time magic.

CLAUDE.md and the session gotchas agree on why: a module-level global
registry makes it impossible to build a registry containing exactly the
fake resolvers a test needs, and a magic auto-discovery mechanism is
untestable. build_registry() is the one explicit construction point.
"""

from __future__ import annotations

from collections.abc import Iterable

from app.resolvers.base import LeadContext, Resolver, Tier


class ResolverRegistry:
    def __init__(self) -> None:
        self._resolvers: list[Resolver] = []

    def register(self, resolver: Resolver) -> None:
        self._resolvers.append(resolver)

    def select(self, *, field: str, tier: Tier, ctx: LeadContext) -> list[Resolver]:
        """Resolvers for this field and tier whose jurisdiction admits this
        lead.

        Jurisdiction is a hard gate here, not a ranking hint: a resolver
        whose `jurisdictions` doesn't include ctx.country_code is never
        returned, at any tier. A US lead can never reach a UK-only
        resolver — no wasted call, and for metered sources, no wasted
        credit.
        """
        return [
            r
            for r in self._resolvers
            if r.field == field
            and r.tier == tier
            and (r.jurisdictions is None or ctx.country_code in r.jurisdictions)
        ]


def build_registry(resolvers: Iterable[Resolver] = ()) -> ResolverRegistry:
    """The one explicit construction point for a registry. Callers pass the
    resolvers they want registered — nothing registers itself as a side
    effect of being imported.
    """
    registry = ResolverRegistry()
    for resolver in resolvers:
        registry.register(resolver)
    return registry
