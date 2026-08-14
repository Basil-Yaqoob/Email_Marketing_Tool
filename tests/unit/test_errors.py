"""Tests for the AppError hierarchy in app/core/errors.py.

BatchAbortedError's message is read in a terminal at 2am after a batch
guard fires — it must be self-explanatory without opening the code.
"""

from __future__ import annotations

from app.core.errors import (
    AppError,
    BatchAbortedError,
    BudgetExceededError,
    MissingConfigError,
    ParseError,
    PolicyBlockedError,
    RateLimitedError,
    ResolverError,
    UpstreamError,
)


def test_missing_config_error_names_the_key() -> None:
    err = MissingConfigError("openrouter_api_key")

    assert err.key == "openrouter_api_key"
    assert "openrouter_api_key" in str(err)
    assert "no default" in str(err)


def test_batch_aborted_error_message_contains_rate_and_counts() -> None:
    """The exact failure mode: 1,640 silent errors. The message must be
    legible on its own — counts, rate, and the last error seen.
    """
    err = BatchAbortedError(completed=42, total=1000, error_rate=0.071, last="timeout")

    message = str(err)
    assert "42" in message
    assert "1000" in message
    assert "7.1%" in message
    assert "timeout" in message
    assert err.completed == 42
    assert err.total == 1000
    assert err.error_rate == 0.071
    assert err.last == "timeout"


def test_resolver_error_subclasses_are_resolver_errors() -> None:
    assert issubclass(UpstreamError, ResolverError)
    assert issubclass(RateLimitedError, ResolverError)
    assert issubclass(ParseError, ResolverError)


def test_all_errors_derive_from_app_error() -> None:
    for cls in (
        MissingConfigError,
        BatchAbortedError,
        ResolverError,
        UpstreamError,
        RateLimitedError,
        ParseError,
        PolicyBlockedError,
        BudgetExceededError,
    ):
        assert issubclass(cls, AppError)
