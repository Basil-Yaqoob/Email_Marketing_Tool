"""MCPClient -- connects to MCP servers over stdio or Streamable HTTP.

Supports both of MCP's transports: stdio for a locally-installed server
(spawned as a subprocess) and Streamable HTTP for a remote one (Apollo,
Clay). A connection's read/write streams and the SDK session that wraps
them are kept alive in an AsyncExitStack on the returned MCPSession, since
list_tools() and call_tool() happen well after connect() returns -- unlike
the SDK's own `async with` examples, this app calls a session repeatedly
across a waterfall run, not once inside a single context block.

A server that is down must degrade, not crash: every method here raises an
app.mcp.errors.MCPError subclass rather than letting a raw transport
exception (ConnectionRefusedError, an httpx error, a JSON-RPC error object)
escape. That is deliberate -- app.mcp.adapter.MCPResolver.resolve() lets
these propagate, and app.resolvers.executor._run_one already turns any
exception from a single resolver into a ResolverAttempt(outcome=ERROR)
instead of failing the whole lead. Coverage is lost; the run survives.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import CallToolResult, TextContent

from app.core.logging import get_logger
from app.mcp.errors import MCPServerUnavailableError, MCPTimeoutError
from app.mcp.types import MCPResult, MCPServerConfig, MCPSession, MCPTool, MCPTransport

log = get_logger(__name__)

# Given a server config, return the headers to authenticate with (e.g. a
# freshly-refreshed OAuth bearer token). Injected rather than imported so
# this module has no dependency on app.mcp.oauth's token-store internals.
AuthHeaderProvider = Callable[[MCPServerConfig], Awaitable[Mapping[str, str] | None]]


class MCPClient:
    """Connects to MCP servers, lists their tools, and calls them."""

    def __init__(self, *, auth_header_provider: AuthHeaderProvider | None = None) -> None:
        self._auth_header_provider = auth_header_provider

    async def connect(self, server: MCPServerConfig) -> MCPSession:
        """Open a session against `server`. Never returns a half-open one:
        on any failure the partially-built stack is closed before raising.
        """
        stack = AsyncExitStack()
        try:
            async with asyncio.timeout(server.timeout_seconds):
                read, write = await self._open_transport(stack, server)
                session = ClientSession(read, write)
                await stack.enter_async_context(session)
                await session.initialize()
        except TimeoutError as exc:
            await stack.aclose()
            raise MCPTimeoutError(server.name, "connect", server.timeout_seconds) from exc
        except Exception as exc:
            # Transport failures arrive as many unrelated types
            # (ConnectionRefusedError, httpx errors, OSError from a missing
            # binary, the SDK's own McpError). Converted to one clear type,
            # never swallowed -- see module docstring.
            await stack.aclose()
            raise MCPServerUnavailableError(server.name, str(exc)) from exc

        log.info("mcp.connected", server=server.name, transport=server.transport.value)
        return MCPSession(server_name=server.name, session=session, _stack=stack)

    async def _open_transport(
        self, stack: AsyncExitStack, server: MCPServerConfig
    ) -> tuple[Any, Any]:
        if server.transport is MCPTransport.STDIO:
            assert server.command is not None  # enforced by MCPServerConfig.__post_init__
            read, write = await stack.enter_async_context(
                stdio_client(
                    StdioServerParameters(
                        command=server.command,
                        args=list(server.args),
                        env=dict(server.env) if server.env else None,
                    )
                )
            )
            return read, write

        assert server.url is not None  # enforced by MCPServerConfig.__post_init__
        headers = await self._auth_headers(server)
        read, write, _get_session_id = await stack.enter_async_context(
            streamablehttp_client(server.url, headers=dict(headers) if headers else None)
        )
        return read, write

    async def _auth_headers(self, server: MCPServerConfig) -> Mapping[str, str] | None:
        if server.static_bearer_token:
            return {"Authorization": f"Bearer {server.static_bearer_token}"}
        if self._auth_header_provider is not None:
            return await self._auth_header_provider(server)
        return None

    async def list_tools(
        self, session: MCPSession, *, timeout_seconds: float = 30.0
    ) -> list[MCPTool]:
        try:
            async with asyncio.timeout(timeout_seconds):
                result = await session.session.list_tools()
        except TimeoutError as exc:
            raise MCPTimeoutError(session.server_name, "list_tools", timeout_seconds) from exc
        except Exception as exc:  # see module docstring: converted, never swallowed
            raise MCPServerUnavailableError(session.server_name, str(exc)) from exc

        return [
            MCPTool(name=t.name, description=t.description or "", input_schema=t.inputSchema)
            for t in result.tools
        ]

    async def call_tool(
        self,
        session: MCPSession,
        name: str,
        args: dict[str, Any],
        *,
        timeout_seconds: float = 30.0,
    ) -> MCPResult:
        """Call one tool. Never hangs the pipeline: a call exceeding
        `timeout_seconds` raises MCPTimeoutError rather than blocking.
        """
        try:
            async with asyncio.timeout(timeout_seconds):
                result = await session.session.call_tool(name, args)
        except TimeoutError as exc:
            raise MCPTimeoutError(session.server_name, name, timeout_seconds) from exc
        except Exception as exc:  # see module docstring: converted, never swallowed
            raise MCPServerUnavailableError(session.server_name, str(exc)) from exc

        if result.isError:
            raise MCPServerUnavailableError(
                session.server_name, _extract_text(result) or f"tool '{name}' returned an error"
            )

        if result.structuredContent is not None:
            return MCPResult(data=result.structuredContent)
        return MCPResult(data=_extract_text(result))


def _extract_text(result: CallToolResult) -> str | None:
    for block in result.content:
        if isinstance(block, TextContent):
            return block.text
    return None


__all__ = ["AuthHeaderProvider", "MCPClient"]
