"""Streamable HTTP auth for the MCP server.

stdio needs no auth -- it's a local subprocess the user's own client (Claude
Desktop, Claude Code) spawned directly; there is no network hop to secure.
Streamable HTTP is reachable over the network, so every request must carry
the same bearer token the REST API uses (Session 20, app.api.auth) -- one
token, two surfaces, one comparison function.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from app.api.auth import InvalidTokenError, verify_bearer_token
from app.core.errors import MissingConfigError


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Rejects any request without a valid `Authorization: Bearer <token>`
    header before it reaches the MCP protocol handler.
    """

    def __init__(self, app: ASGIApp, *, configured_token: str | None) -> None:
        super().__init__(app)
        self._configured_token = configured_token

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        authorization = request.headers.get("authorization")
        token = (
            authorization[len("Bearer ") :]
            if authorization and authorization.startswith("Bearer ")
            else None
        )
        try:
            verify_bearer_token(token, configured=self._configured_token)
        except MissingConfigError:
            return JSONResponse(
                {"error": "server has no api_token configured -- Streamable HTTP is disabled"},
                status_code=503,
            )
        except InvalidTokenError:
            return JSONResponse({"error": "missing or invalid bearer token"}, status_code=401)

        return await call_next(request)


def with_bearer_auth(app: ASGIApp, *, configured_token: str | None) -> ASGIApp:
    """Wrap `app` so every request must carry the configured bearer token."""
    return BearerAuthMiddleware(app, configured_token=configured_token)


__all__ = ["BearerAuthMiddleware", "with_bearer_auth"]
