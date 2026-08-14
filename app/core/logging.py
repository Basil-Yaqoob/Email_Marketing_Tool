"""Structured JSON logging.

CLAUDE.md rule 6: structured logging everywhere, never an f-string message.
`log.info("resolver.hit", resolver="osm", count=12)`, not
`log.info(f"osm hit with {12} results")` — the analytics phase (Session 19)
reads these logs as data, not as text to grep.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, cast

import structlog

from app.core.config import Settings


def configure_logging(settings: Settings) -> None:
    """Set up structlog to emit JSON to stdout at the configured level.

    Call this once, at process start, before any other module logs. stdio
    MCP servers (Session 23) must route logging to stderr instead — that is
    the caller's responsibility, not this function's, since most entry
    points (API, worker) want stdout.
    """
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    # force=True: without it, basicConfig() is a no-op after the first call
    # in a process (e.g. a second campaign run, or a test re-configuring at
    # a different level), silently keeping the old level and stream.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
        force=True,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


def bind_context(**kwargs: Any) -> None:
    """Bind fields (e.g. campaign_id) onto every subsequent log call on this
    context/task, until `clear_context` is called.
    """
    structlog.contextvars.bind_contextvars(**kwargs)


def clear_context() -> None:
    """Clear context-bound fields. Call at the end of a job/request."""
    structlog.contextvars.clear_contextvars()


def get_logger(*args: Any, **kwargs: Any) -> structlog.stdlib.BoundLogger:
    """Thin wrapper so call sites do `from app.core.logging import get_logger`
    instead of importing structlog directly everywhere.
    """
    # structlog.get_logger() is typed to return Any; cast to the bound-logger
    # type we actually configure in configure_logging().
    return cast("structlog.stdlib.BoundLogger", structlog.get_logger(*args, **kwargs))
