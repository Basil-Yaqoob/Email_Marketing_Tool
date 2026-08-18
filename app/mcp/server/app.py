"""Builds and runs the MCP server -- the platform exposed over MCP.

Supports both of MCP's transports: stdio (Claude Desktop, Claude Code --
see README's config block) and Streamable HTTP (a remote client). Tools and
resources are registered against one PlatformService instance -- see
app.mcp.server.service for why that boundary exists, and
app.mcp.server.memory_service for what actually backs it today.

stdio servers must not write anything to stdout except protocol messages; a
stray print() corrupts the stream. app.core.logging routes structlog
through the standard logging module (not print()), so nothing in this
codepath writes to stdout except the SDK's own protocol layer.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from starlette.types import ASGIApp

from app.core.config import Settings
from app.mcp.server.auth import with_bearer_auth
from app.mcp.server.resources import register_resources
from app.mcp.server.service import PlatformService
from app.mcp.server.tools import register_tools

SERVER_NAME = "email-marketing-tool"

SERVER_INSTRUCTIONS = (
    "Self-hosted cold email platform. Read the docs://limits resource before making "
    "claims about coverage or completeness -- it states the platform's real ceilings "
    "(e.g. 55-70% decision-maker email coverage, UNKNOWN as a normal SMTP-verification "
    "outcome for Google Workspace/Microsoft 365 domains, never as a failure). "
    "launch_campaign sends real email and cannot be undone; never call it with "
    "confirm=true without the user's explicit, specific approval to send."
)


def build_server(service: PlatformService) -> FastMCP:
    """Construct the FastMCP server with every tool and resource registered
    against `service`. Called once per process (or once per test session).
    """
    mcp = FastMCP(SERVER_NAME, instructions=SERVER_INSTRUCTIONS)
    register_tools(mcp, service)
    register_resources(mcp, service)
    return mcp


async def run_stdio(service: PlatformService) -> None:
    """Entry point for Claude Desktop / Claude Code. No auth: the client
    spawned this process directly, so there is no network hop to secure.
    """
    mcp = build_server(service)
    await mcp.run_stdio_async()


def build_streamable_http_app(service: PlatformService, settings: Settings) -> ASGIApp:
    """The auth-wrapped ASGI app for Streamable HTTP.

    Kept separate from a `run_streamable_http` that binds a real port, so a
    test can drive requests against this app directly (via an ASGI test
    transport) without opening a socket.
    """
    mcp = build_server(service)
    configured_token = settings.api_token.get_secret_value() if settings.api_token else None
    return with_bearer_auth(mcp.streamable_http_app(), configured_token=configured_token)


__all__ = ["SERVER_NAME", "build_server", "build_streamable_http_app", "run_stdio"]
