"""Consistent error envelope mapping app exceptions to HTTP status codes.

Every error follows this shape:
  {
    "error": {
      "code": "policy_blocked",
      "message": "Human-readable explanation",
      "details": [{"field": "address", "issue": "missing"}],
      "request_id": "uuid"
    }
  }

Exception → HTTP status mapping:
  MissingConfigError → 503 (service unavailable)
  BatchAbortedError → 409 (conflict: batch guard fired)
  RateLimitedError → 429 (too many requests)
  UpstreamError → 502 (bad gateway)
  PolicyBlockedError → 422 (unprocessable entity)
  BudgetExceededError → 402 (payment required)
"""

from __future__ import annotations

from uuid import uuid4

from pydantic import BaseModel, Field

from app.core.errors import (
    BatchAbortedError,
    BudgetExceededError,
    MissingConfigError,
    PolicyBlockedError,
    RateLimitedError,
    UpstreamError,
)


class ErrorDetail(BaseModel):
    """Detail about a specific error condition."""

    field: str | None = None
    issue: str = ""
    message_id: str | None = None
    check: str | None = None


class ErrorEnvelope(BaseModel):
    """Consistent error response envelope."""

    code: str
    message: str
    details: list[ErrorDetail] = Field(default_factory=list)
    request_id: str


class APIError(BaseModel):
    """Top-level error response."""

    error: ErrorEnvelope


def make_error_envelope(
    code: str,
    message: str,
    details: list[ErrorDetail] | None = None,
    request_id: str | None = None,
) -> ErrorEnvelope:
    """Create an error envelope.

    Args:
        code: Machine-readable error code
        message: Human-readable message (not f-string from scraped content)
        details: Optional list of field-level details
        request_id: Optional request ID (auto-generated if not provided)

    Returns:
        ErrorEnvelope ready to send
    """
    return ErrorEnvelope(
        code=code,
        message=message,
        details=details or [],
        request_id=request_id or str(uuid4()),
    )


def exception_to_status(exc: Exception) -> tuple[int, ErrorEnvelope]:
    """Map an app exception to HTTP status and error envelope.

    Args:
        exc: The exception that was raised

    Returns:
        (status_code, ErrorEnvelope)
    """
    code_map: dict[type[Exception], tuple[int, str]] = {
        MissingConfigError: (503, "service_unavailable"),
        BatchAbortedError: (409, "batch_aborted"),
        RateLimitedError: (429, "rate_limited"),
        UpstreamError: (502, "upstream_error"),
        PolicyBlockedError: (422, "policy_blocked"),
        BudgetExceededError: (402, "budget_exceeded"),
    }

    status, code = code_map.get(type(exc), (500, "internal_error"))
    message = str(exc)

    return status, make_error_envelope(code=code, message=message)


__all__ = ["APIError", "ErrorDetail", "ErrorEnvelope", "exception_to_status", "make_error_envelope"]
