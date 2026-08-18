"""MCP-specific exceptions.

All subclass AppError, so app.resolvers.executor's per-resolver broad catch
(app/resolvers/executor.py:_run_one) turns any of these into a ResolverAttempt
with outcome=ERROR instead of crashing the waterfall for the whole lead. That
is the mechanism behind "a server that is down must degrade, not crash" --
no special-casing needed at the adapter, the existing fail-loud-at-the-batch,
soft-fail-at-the-resolver split already does the right thing.
"""

from __future__ import annotations

from app.core.errors import AppError


class MCPError(AppError):
    """Base for everything the MCP client layer raises deliberately."""


class MCPServerUnavailableError(MCPError):
    """Connecting to, or calling, an MCP server failed.

    Covers connection refused, transport errors, and the server returning
    a JSON-RPC error -- anything that means "this call didn't produce an
    answer", as opposed to "the answer says nothing was found".
    """

    def __init__(self, server_name: str, detail: str) -> None:
        super().__init__(f"MCP server '{server_name}' unavailable: {detail}")
        self.server_name = server_name


class MCPTimeoutError(MCPError):
    """A call exceeded its configured timeout.

    Raised instead of letting the call hang -- a slow metered server must
    cost time, not stall the whole batch.
    """

    def __init__(self, server_name: str, tool_name: str, timeout_seconds: float) -> None:
        super().__init__(
            f"MCP call '{tool_name}' on '{server_name}' exceeded {timeout_seconds}s timeout"
        )
        self.server_name = server_name
        self.tool_name = tool_name


class MCPSchemaError(MCPError):
    """A server's advertised tool schema is missing or doesn't match what
    the adapter configuration expects.

    Raised at connect time, not at call time -- a schema change should
    surface as a clear "this integration needs updating" message, not a
    confusing failure three fields into building the call arguments.
    """


__all__ = ["MCPError", "MCPSchemaError", "MCPServerUnavailableError", "MCPTimeoutError"]
