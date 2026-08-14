"""Fixtures for integration tests: a real Postgres, not a mock.

Tests run against a dedicated `<dev-db>_test` database, never the dev
database itself, so `make check` can't quietly eat dev data. The schema is
migrated once per test session via the real Alembic migration (proving it
actually applies — see test_migrations_apply_and_rollback_cleanly for the
separate, throwaway-database proof that it also downgrades cleanly); each
test then runs inside its own transaction that is always rolled back, so
tests never see each other's writes without needing a re-migration per test.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncGenerator
from pathlib import Path

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings

ROOT = Path(__file__).resolve().parents[2]


def _test_database_url(settings: Settings) -> str:
    """<dev-db-url>_test, unless TEST_DATABASE_URL overrides it explicitly."""
    override = os.environ.get("TEST_DATABASE_URL")
    if override:
        return override
    base, _, dbname = settings.database_url.rpartition("/")
    return f"{base}/{dbname}_test"


async def _ensure_database_exists(url: str) -> None:
    """CREATE DATABASE cannot run inside a transaction, so this connects
    directly with asyncpg against the `postgres` maintenance database
    rather than going through a SQLAlchemy-managed connection.
    """
    parsed = make_url(url)
    conn = await asyncpg.connect(
        user=parsed.username,
        password=parsed.password,
        host=parsed.host,
        port=parsed.port or 5432,
        database="postgres",
    )
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", parsed.database
        )
        if not exists:
            # Database names can't be parameterised; quote defensively since
            # this is derived from a config-owned URL, not user input.
            await conn.execute(f'CREATE DATABASE "{parsed.database}"')
    finally:
        await conn.close()


def _alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "app" / "db" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture(scope="session")
def test_database_url() -> str:
    return _test_database_url(Settings())


@pytest.fixture(scope="session", autouse=True)
async def _migrated_schema(test_database_url: str) -> None:
    """Session-scoped: create the test database if needed and apply every
    migration exactly once. Runs automatically for any test under
    tests/integration/. No teardown — the schema is left in place for
    Alembic to detect as already-current on the next test run.
    """
    await _ensure_database_exists(test_database_url)
    cfg = _alembic_config(test_database_url)
    # command.upgrade() runs env.py, which calls asyncio.run() internally
    # for the async engine — that raises "cannot be called from a running
    # event loop" if invoked directly from this async fixture. Running it
    # in a worker thread gives it a thread with no running loop of its own.
    await asyncio.to_thread(command.upgrade, cfg, "head")


@pytest.fixture
async def db_session(test_database_url: str) -> AsyncGenerator[AsyncSession]:
    """One AsyncSession per test, bound to a connection whose outer
    transaction is always rolled back at teardown — test isolation without
    re-running migrations for every test.
    """
    engine = create_async_engine(test_database_url)
    async with engine.connect() as conn:
        outer = await conn.begin()
        sessionmaker = async_sessionmaker(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        async with sessionmaker() as session:
            yield session
        await outer.rollback()
    await engine.dispose()
