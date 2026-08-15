"""Structural guarantee, not a style preference: no resolver in this
codebase may ever issue an HTTP request to linkedin.com or instagram.com.

CLAUDE.md §10: "No safe automated LinkedIn scraping exists... Never build
cookie-based LinkedIn scraping into the product." SERPPersonResolver and
InstagramEmailResolver get everything they need from a search index's
own snippet text — the profile page itself is never fetched. This test
proves that architecturally: any request to either host, from any
resolver, fails the test immediately via a respx side_effect that raises
before the request can even "succeed".

Do not delete this file to make a future change easier — that is exactly
the kind of change it exists to catch.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import uuid4

import pytest
import respx

from app.net.search import SearchResult
from app.resolvers.base import LeadContext, Tier
from app.resolvers.company.instagram import InstagramEmailResolver
from app.resolvers.person.serp import SERPPersonResolver


def _forbidden_host_guard() -> None:
    """Any request whose host contains linkedin.com or instagram.com
    fails immediately, from inside respx's own resolution — so even a
    request our code never awaits the response of still trips this.
    """

    def _raise(request: object) -> None:
        raise AssertionError(
            f"Forbidden request to {request!r} — see CLAUDE.md §10: "
            "no code path may ever fetch linkedin.com or instagram.com."
        )

    respx.route(url__regex=r".*linkedin\.com.*").mock(side_effect=_raise)
    respx.route(url__regex=r".*instagram\.com.*").mock(side_effect=_raise)


def _ctx(**overrides: object) -> LeadContext:
    defaults: dict[str, object] = {
        "company_id": uuid4(),
        "company_name": "Northgate Dental",
        "domain": "northgatedental.com",
        "website": "https://northgatedental.com",
        "country_code": "US",
    }
    defaults.update(overrides)
    return LeadContext(**defaults)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class _FakeSearchBackend:
    """A SearchBackend double that returns LinkedIn/Instagram-flavoured
    *snippet text* — the temptation this guard exists to resist — without
    ever making a real HTTP request anywhere. If the resolver under test
    were to (bug!) turn around and fetch one of those URLs for real, the
    respx guard above catches it.
    """

    name: str = "fake"
    tier: Tier = Tier.FREE
    cost_per_call: Decimal = Decimal("0")

    async def search(self, query: str, *, limit: int = 10) -> list[SearchResult]:
        return [
            SearchResult(
                title="Jane Smith - CEO - Northgate Dental | LinkedIn",
                url="https://www.linkedin.com/in/janesmith",
                snippet="Jane Smith is the CEO of Northgate Dental in Austin, TX.",
                position=0,
            ),
            SearchResult(
                title="Northgate Dental (@northgatedental) • Instagram",
                url="https://www.instagram.com/northgatedental/",
                snippet="Book online or email us at hello@northgatedental.com",
                position=1,
            ),
        ]


@respx.mock
async def test_no_resolver_can_request_linkedin_dot_com() -> None:
    """The architectural guard for SERPPersonResolver."""
    _forbidden_host_guard()
    resolver = SERPPersonResolver(search=_FakeSearchBackend())

    candidates = await resolver.resolve(_ctx())

    # Reaching this line at all (without respx's AssertionError firing)
    # is the proof. The resolver should also have done its job correctly
    # using only the snippet text it was handed.
    assert candidates
    assert candidates[0].value == "Jane Smith"
    assert candidates[0].source_url == "https://www.linkedin.com/in/janesmith"


@respx.mock
async def test_instagram_resolver_never_fetches_instagram_com() -> None:
    """The architectural guard for InstagramEmailResolver — same shape."""
    _forbidden_host_guard()
    resolver = InstagramEmailResolver(search=_FakeSearchBackend())

    candidates = await resolver.resolve(_ctx())

    assert candidates
    assert candidates[0].value == "hello@northgatedental.com"
    assert candidates[0].source_url == "https://www.instagram.com/northgatedental/"


@respx.mock
async def test_guard_actually_fires_on_a_real_attempt() -> None:
    """Proves the guard itself isn't a no-op: a resolver that DID try to
    fetch linkedin.com would fail loudly, not silently pass.
    """
    _forbidden_host_guard()

    class _BadBackend:
        name = "bad"
        tier = Tier.FREE
        cost_per_call = Decimal("0")

        async def search(self, query: str, *, limit: int = 10) -> list[SearchResult]:
            import httpx

            async with httpx.AsyncClient() as client:
                await client.get("https://www.linkedin.com/in/janesmith")
            return []

    with pytest.raises(AssertionError, match="Forbidden request"):
        await _BadBackend().search("anything")
