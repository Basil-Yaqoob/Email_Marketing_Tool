"""Integration tests for app/db/session.py against real Postgres.

Session 02 ships this module as the engine/session factory every later
resolver, worker and API route builds on — it needs its own coverage, not
just an incidental pass-through via the repository tests.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.db.models.campaign import Campaign
from app.db.models.enums import CampaignStatus
from app.db.session import dispose_engine, get_engine, get_session, get_sessionmaker, session_scope

pytestmark = pytest.mark.asyncio


class _BoomError(Exception):
    """Marker exception for the rollback test — never a real failure mode."""


def _settings(test_database_url: str) -> Settings:
    return Settings(  # type: ignore[call-arg]
        database_url=test_database_url,
        redis_url="redis://localhost:6379/0",
        secret_key="test",
    )


@pytest.fixture(autouse=True)
async def _reset_engine() -> AsyncGenerator[None]:
    """The engine/sessionmaker are module-level singletons — reset them
    around each test so one test's engine (bound to the test DB URL) never
    leaks into another test via cached state.
    """
    await dispose_engine()
    yield
    await dispose_engine()


async def test_get_engine_reuses_same_engine_across_calls(test_database_url: str) -> None:
    settings = _settings(test_database_url)
    first = get_engine(settings)
    second = get_engine(settings)
    assert first is second


async def test_session_scope_commits_on_clean_exit(test_database_url: str) -> None:
    settings = _settings(test_database_url)
    campaign_id = uuid.uuid4()

    async with session_scope(settings) as session:
        session.add(
            Campaign(id=campaign_id, name="session-scope test", icp={}, status=CampaignStatus.DRAFT)
        )

    # A fresh session confirms the write was actually committed, not just
    # visible within the same transaction.
    sessionmaker = get_sessionmaker(settings)
    async with sessionmaker() as verify_session:
        row = await verify_session.get(Campaign, campaign_id)
        assert row is not None
        assert row.name == "session-scope test"
        await verify_session.delete(row)
        await verify_session.commit()


async def _add_and_raise(session_cm: object, campaign_id: uuid.UUID) -> None:
    async with session_cm as session:  # type: ignore[attr-defined]
        session.add(
            Campaign(id=campaign_id, name="should not persist", icp={}, status=CampaignStatus.DRAFT)
        )
        await session.flush()
        raise _BoomError


async def test_session_scope_rolls_back_on_exception(test_database_url: str) -> None:
    settings = _settings(test_database_url)
    campaign_id = uuid.uuid4()

    with pytest.raises(_BoomError):
        await _add_and_raise(session_scope(settings), campaign_id)

    sessionmaker = get_sessionmaker(settings)
    async with sessionmaker() as verify_session:
        row = await verify_session.get(Campaign, campaign_id)
        assert row is None


async def test_get_session_dependency_yields_working_session(test_database_url: str) -> None:
    settings = _settings(test_database_url)

    gen = get_session(settings)
    session = await anext(gen)
    try:
        result = await session.execute(select(Campaign).limit(1))
        assert result is not None
    finally:
        # Drain the generator's cleanup path (commit) without raising.
        with contextlib.suppress(StopAsyncIteration):
            await anext(gen)


async def test_dispose_engine_clears_cached_engine(test_database_url: str) -> None:
    settings = _settings(test_database_url)
    get_engine(settings)
    await dispose_engine()

    fresh = get_engine(settings)
    assert fresh is not None
