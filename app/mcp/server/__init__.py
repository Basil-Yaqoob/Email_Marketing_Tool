"""This app as an MCP server -- exposes campaigns, leads, and messages as
MCP tools/resources so a client (Claude Desktop, Claude Code) can run a
campaign conversationally. See app.mcp.server.app.build_server.
"""

from app.mcp.server.app import build_server, build_streamable_http_app, run_stdio

__all__ = ["build_server", "build_streamable_http_app", "run_stdio"]
