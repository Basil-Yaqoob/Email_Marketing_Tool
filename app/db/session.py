"""Async engine and session factory.

Two entry points on purpose: `get_session()` is a FastAPI dependency
(Session 20), `session_scope()` is a plain async context manager for
workers (arq tasks) and scripts that aren't inside a request.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine(settings: Settings) -> AsyncEngine:
    """Lazily create the module-level engine, reusing it across calls.

    One engine per process — SQLAlchemy's async engine already pools
    connections internally, so a fresh engine per call would leak.
    """
    global _engine
    if _engine is None:
        _engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    return _engine


def get_sessionmaker(settings: Settings) -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            get_engine(settings), expire_on_commit=False, autoflush=False
        )
    return _sessionmaker


async def get_session(settings: Settings) -> AsyncGenerator[AsyncSession]:
    """FastAPI dependency: `session: AsyncSession = Depends(get_session)`.

    Commits on clean exit, rolls back on exception, always closes.
    """
    sessionmaker = get_sessionmaker(settings)
    async with sessionmaker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def session_scope(settings: Settings) -> AsyncIterator[AsyncSession]:
    """Plain async context manager for workers and scripts.

    `async with session_scope(settings) as session: ...`
    """
    sessionmaker = get_sessionmaker(settings)
    async with sessionmaker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Close all pooled connections. Call on process shutdown and between
    test runs that swap out the database URL.
    """
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
