"""Shared bearer-token verification.

One mechanism, two callers: the REST API (Session 20) and the MCP server's
Streamable HTTP transport (Session 23) both authenticate the same way, so
the comparison lives here once instead of being copied.

Comparison is constant-time (`secrets.compare_digest`) so a request can't
learn anything about the configured token from response timing. A server
with no token configured refuses every request rather than accepting
anything -- CLAUDE.md rule 2.4: no secret gets a fallback that makes it
optional in practice.
"""

from __future__ import annotations

import secrets

from app.core.errors import MissingConfigError


class InvalidTokenError(Exception):
    """The presented bearer token did not match the configured one."""


def verify_bearer_token(token: str | None, *, configured: str | None) -> None:
    """Raise if `token` doesn't match `configured`.

    Args:
        token: The token presented by the caller, already stripped of the
            "Bearer " prefix. None if no token was presented at all.
        configured: The real token from settings, or None if the deployment
            never set one.

    Raises:
        MissingConfigError: no token is configured -- the surface calling
            this must treat that as "not available", not "open to anyone".
        InvalidTokenError: a token was configured but it doesn't match.
    """
    if configured is None:
        raise MissingConfigError("api_token")
    matches = token is not None and secrets.compare_digest(
        token.encode("utf-8"), configured.encode("utf-8")
    )
    if not matches:
        raise InvalidTokenError("Invalid token")


__all__ = ["InvalidTokenError", "verify_bearer_token"]
