"""OAuth 2.0 Authorization Code flow with PKCE, for MCP servers that need it.

Apollo's hosted server (mcp.apollo.io/mcp) authenticates this way. PKCE
means no client secret has to be embedded anywhere reachable by the
redirect -- the verifier never leaves this process until the token
exchange, and the challenge sent up front is a one-way hash of it.

Tokens are encrypted at rest via Session 14's CredentialVault (the same
mechanism mailbox passwords use) and never logged: OAuthToken's __repr__
redacts the access and refresh tokens, so even an accidental
`log.info("...", token=token)` or an uncaught exception's repr in a log
line can't leak one.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import urlencode

import httpx

from app.core.logging import get_logger
from app.mcp.errors import MCPServerUnavailableError
from app.sending.credentials import CredentialVault

log = get_logger(__name__)

# How long before real expiry to treat a token as needing refresh -- avoids a
# race where a call starts against a token that expires mid-flight.
REFRESH_BUFFER_SECONDS = 60


@dataclass(frozen=True, slots=True)
class OAuthConfig:
    """Everything needed to run the Authorization Code + PKCE flow for one
    server, and to refresh tokens afterward.
    """

    server_name: str
    authorize_url: str
    token_url: str
    client_id: str
    client_secret: str | None  # public (PKCE-only) clients may omit this
    redirect_uri: str
    scope: str | None = None


@dataclass(frozen=True, slots=True)
class PKCEPair:
    verifier: str
    challenge: str  # S256(verifier), base64url, no padding


def generate_pkce_pair() -> PKCEPair:
    """RFC 7636: a high-entropy verifier, and its S256 challenge."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return PKCEPair(verifier=verifier, challenge=challenge)


@dataclass(frozen=True, slots=True)
class OAuthToken:
    access_token: str
    refresh_token: str | None
    expires_at: datetime
    token_type: str = "Bearer"

    def is_expired(self, *, buffer_seconds: int = REFRESH_BUFFER_SECONDS) -> bool:
        return datetime.now(UTC) >= self.expires_at - timedelta(seconds=buffer_seconds)

    def __repr__(self) -> str:
        expires = self.expires_at.isoformat()
        return f"OAuthToken(access_token='***redacted***', expires_at={expires!r})"

    __str__ = __repr__


def build_authorization_url(config: OAuthConfig, *, state: str, code_challenge: str) -> str:
    """The URL to send the user to. `state` must be checked against the
    callback to prevent CSRF -- generate it with secrets.token_urlsafe()
    and store it alongside the PKCE verifier for the duration of the flow.
    """
    params = {
        "response_type": "code",
        "client_id": config.client_id,
        "redirect_uri": config.redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if config.scope:
        params["scope"] = config.scope
    return f"{config.authorize_url}?{urlencode(params)}"


async def exchange_code_for_token(
    config: OAuthConfig,
    *,
    code: str,
    code_verifier: str,
    http_client: httpx.AsyncClient,
) -> OAuthToken:
    """The callback handler's job: turn an authorization code plus the PKCE
    verifier into a token. Never logs `code` or the resulting token.
    """
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": config.redirect_uri,
        "client_id": config.client_id,
        "code_verifier": code_verifier,
    }
    if config.client_secret:
        data["client_secret"] = config.client_secret

    try:
        response = await http_client.post(config.token_url, data=data)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise MCPServerUnavailableError(
            config.server_name, f"token exchange failed: {exc}"
        ) from exc

    return _token_from_response(response.json())


async def refresh_if_needed(
    config: OAuthConfig, token: OAuthToken, *, http_client: httpx.AsyncClient
) -> OAuthToken:
    """Return `token` unchanged if it's still fresh; otherwise refresh it.

    This is the function callers should call before every use, not just on
    a schedule -- "before expiry" per the session plan, checked at the
    point of use rather than trusted to a background timer that could drift.
    """
    if not token.is_expired():
        return token
    if not token.refresh_token:
        raise MCPServerUnavailableError(
            config.server_name, "access token expired and no refresh_token is available"
        )

    data = {
        "grant_type": "refresh_token",
        "refresh_token": token.refresh_token,
        "client_id": config.client_id,
    }
    if config.client_secret:
        data["client_secret"] = config.client_secret

    try:
        response = await http_client.post(config.token_url, data=data)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise MCPServerUnavailableError(config.server_name, f"token refresh failed: {exc}") from exc

    log.info("mcp.oauth.token_refreshed", server=config.server_name)
    return _token_from_response(response.json())


def _token_from_response(payload: dict[str, object]) -> OAuthToken:
    expires_in = int(payload.get("expires_in", 3600))  # type: ignore[call-overload]
    refresh_token = payload.get("refresh_token")
    return OAuthToken(
        access_token=str(payload["access_token"]),
        refresh_token=str(refresh_token) if refresh_token else None,
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
        token_type=str(payload.get("token_type", "Bearer")),
    )


class TokenStore(Protocol):
    """Where OAuth tokens live between calls.

    Discovered work: no `mcp_tokens` table exists yet -- the same gap
    Session 11 left for `llm_calls` (see doc/02-ROADMAP.md's Discovered
    work table). EncryptedTokenStore below is the only implementation
    shipped this session; it is in-memory, so a process restart forces a
    fresh OAuth flow. Real persistence is a small additive migration for
    whichever session first runs this against a live server long enough
    for that to matter -- the Protocol boundary means no caller changes
    when it lands.
    """

    async def get(self, server_name: str) -> OAuthToken | None: ...
    async def save(self, server_name: str, token: OAuthToken) -> None: ...


class EncryptedTokenStore:
    """In-memory TokenStore that still exercises the real encryption path:
    tokens are Fernet-encrypted via CredentialVault before being held, so
    nothing -- a memory dump, an accidental repr of internal state -- ever
    holds a raw access token outside the brief moment it's decrypted for
    use.
    """

    def __init__(self, vault: CredentialVault) -> None:
        self._vault = vault
        self._tokens: dict[str, _EncryptedEntry] = {}

    async def save(self, server_name: str, token: OAuthToken) -> None:
        self._tokens[server_name] = _EncryptedEntry(
            access_token=self._vault.encrypt(token.access_token),
            refresh_token=self._vault.encrypt(token.refresh_token) if token.refresh_token else None,
            expires_at=token.expires_at,
            token_type=token.token_type,
        )

    async def get(self, server_name: str) -> OAuthToken | None:
        entry = self._tokens.get(server_name)
        if entry is None:
            return None
        return OAuthToken(
            access_token=self._vault.decrypt(entry.access_token),
            refresh_token=self._vault.decrypt(entry.refresh_token) if entry.refresh_token else None,
            expires_at=entry.expires_at,
            token_type=entry.token_type,
        )


@dataclass(frozen=True, slots=True)
class _EncryptedEntry:
    access_token: bytes
    refresh_token: bytes | None
    expires_at: datetime
    token_type: str


__all__ = [
    "EncryptedTokenStore",
    "OAuthConfig",
    "OAuthToken",
    "PKCEPair",
    "TokenStore",
    "build_authorization_url",
    "exchange_code_for_token",
    "generate_pkce_pair",
    "refresh_if_needed",
]
