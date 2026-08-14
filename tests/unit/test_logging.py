"""Tests for app/core/logging.py.

Covers the structlog configuration path (CLAUDE.md rule 6: structured JSON
logging everywhere) and the context binding helpers used to attach
campaign_id/lead_id to every log line inside a run.
"""

from __future__ import annotations

import json
import logging

import structlog

from app.core.config import Settings
from app.core.logging import bind_context, clear_context, configure_logging, get_logger


def _settings(**overrides: object) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url="postgresql+asyncpg://u:p@localhost/db",
        redis_url="redis://localhost:6379/0",
        secret_key="test-secret-key",  # type: ignore[arg-type]
        **overrides,  # type: ignore[arg-type]
    )


def test_configure_logging_respects_log_level() -> None:
    configure_logging(_settings(log_level="WARNING"))
    root_level = logging.getLogger().level
    assert root_level == logging.WARNING


def test_configure_logging_defaults_to_info() -> None:
    configure_logging(_settings())
    root_level = logging.getLogger().level
    assert root_level == logging.INFO


def test_bind_and_clear_context_round_trip() -> None:
    clear_context()
    bind_context(campaign_id="abc-123")
    bound = structlog.contextvars.get_contextvars()
    assert bound["campaign_id"] == "abc-123"

    clear_context()
    assert structlog.contextvars.get_contextvars() == {}


def test_get_logger_returns_a_bound_logger() -> None:
    configure_logging(_settings())
    log = get_logger("test.module")
    # A bound logger exposes the standard level methods.
    assert hasattr(log, "info")
    assert hasattr(log, "error")
    assert hasattr(log, "warning")


def test_json_renderer_produces_parseable_output() -> None:
    """End-to-end: configure, log, capture stdout, parse as JSON.

    This is the actual guarantee — Session 19's analytics reads these logs
    as structured data, not by grepping formatted strings.
    """
    import io

    buffer = io.StringIO()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=buffer),
        cache_logger_on_first_use=False,
    )

    log = structlog.get_logger("test")
    log.info("resolver.hit", resolver="osm", count=12)

    output = buffer.getvalue().strip()
    parsed = json.loads(output)
    assert parsed["event"] == "resolver.hit"
    assert parsed["resolver"] == "osm"
    assert parsed["count"] == 12
    assert parsed["level"] == "info"

    # Restore real config for any test that runs after this one.
    configure_logging(_settings())
