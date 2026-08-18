"""python -m app.mcp.server -- stdio entry point for Claude Desktop / Claude
Code. See README.md's "MCP server" section for the config block.

Runs against app.mcp.server.memory_service.InMemoryPlatformService, a
seeded reference implementation -- not the production data layer. See that
module's docstring for what a real implementation needs.
"""

from __future__ import annotations

import anyio

from app.mcp.server.app import run_stdio
from app.mcp.server.memory_service import InMemoryPlatformService


def main() -> None:
    service = InMemoryPlatformService()
    anyio.run(run_stdio, service)


if __name__ == "__main__":
    main()
