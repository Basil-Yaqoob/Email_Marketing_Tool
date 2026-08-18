"""Transport-agnostic MCP types.

These wrap the official SDK's objects rather than exposing them directly, so
the rest of the app (the adapter, the resolvers, tests) depends on a small
stable shape instead of the SDK's session/stream internals. See
app/mcp/client.py for the one place that talks to the SDK.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from mcp import ClientSession


class MCPTransport(enum.StrEnum):
    """How to reach a server. stdio is a local subprocess (a server the
    user installed themselves); STREAMABLE_HTTP is a remote server like
    Apollo's or Clay's.
    """

    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable_http"


@dataclass(frozen=True, slots=True)
class MCPServerConfig:
    """Everything needed to connect to one MCP server.

    Deliberately a plain config object, not a class per server -- the whole
    point of app/mcp/adapter.py is that a new server needs one of these plus
    a mapping, not new code.
    """

    name: str
    transport: MCPTransport
    # stdio
    command: str | None = None
    args: tuple[str, ...] = ()
    env: Mapping[str, str] | None = None
    # streamable_http
    url: str | None = None
    oauth_server_name: str | None = None  # key into the token store, if this
    # server needs OAuth (app/mcp/oauth.py); None means no auth, or a static
    # bearer token supplied via `static_bearer_token`.
    static_bearer_token: str | None = None
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.transport is MCPTransport.STDIO and not self.command:
            raise ValueError(f"MCP server '{self.name}': stdio transport needs a command")
        if self.transport is MCPTransport.STREAMABLE_HTTP and not self.url:
            raise ValueError(f"MCP server '{self.name}': streamable_http transport needs a url")


@dataclass(frozen=True, slots=True)
class MCPTool:
    """One tool a server advertises."""

    name: str
    description: str
    input_schema: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class MCPResult:
    """The result of one tool call, decoded from the SDK's content blocks
    into plain data the adapter can walk without knowing about MCP's wire
    format.
    """

    data: Any  # parsed structured content, or the raw text if unstructured
    is_error: bool = False


@dataclass(slots=True)
class MCPSession:
    """A live connection to one server. Holds the resources that must be
    torn down together (the SDK session plus whatever transport streams and
    subprocess/HTTP client it opened) behind one close(), so a caller never
    has to know the shutdown order.
    """

    server_name: str
    session: ClientSession
    _stack: AsyncExitStack = field(repr=False)

    async def close(self) -> None:
        await self._stack.aclose()

    async def __aenter__(self) -> MCPSession:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()


__all__ = [
    "MCPResult",
    "MCPServerConfig",
    "MCPSession",
    "MCPTool",
    "MCPTransport",
]
