# Email Marketing Tool

A self-hosted, local-first cold email platform. See `CLAUDE.md` for the
full project context, architecture and non-negotiable rules.

## Setup

```bash
uv sync                                # install dependencies
uv run playwright install chromium     # browser binary for JS-heavy pages
cp .env.example .env                   # fill in the required keys
docker compose up -d postgres redis
uv run alembic upgrade head
```

`uv run playwright install chromium` downloads a Chromium binary Playwright
manages itself — separate from any browser already on your machine. It's
needed by `app/net/browser.py`, the fallback used when a page needs
JavaScript to render (Session 06's crawler decides when to escalate to it).
Skipping this step is fine until something actually calls `BrowserFetcher`;
every other test and resolver works without it.

## Commands

```bash
uv run pytest                    # all tests
uv run pytest tests/unit -q      # fast loop, no Postgres needed
uv run pytest --cov=app --cov-report=term-missing
uv run ruff check . && uv run ruff format --check .
uv run mypy app
uv run uvicorn app.main:app --reload
uv run arq app.workers.WorkerSettings
make check                       # lint + types + tests; must pass before any commit
```

## MCP

This platform is both an MCP *client* (paid data sources join the waterfall
as metered resolvers) and an MCP *server* (the platform itself is drivable
from Claude Desktop or Claude Code).

### Client: Apollo and Clay as metered resolvers

Configured entirely through `.env` — see `.env.example`'s MCP section.
Nothing is registered until `MCP_SPEND_CAP_USD` is set (CLAUDE.md rule 2.3:
free before paid, always — no cap means no metered calls at all, not an
uncapped one). Apollo needs an OAuth client id from its MCP dashboard and a
one-time interactive authorization; Clay needs its hosted server URL and an
API key.

### Server: driving the platform from Claude Desktop / Claude Code

Add this to Claude Desktop's `claude_desktop_config.json` (or Claude Code's
MCP config) to run the server locally over stdio — no auth needed, since
the client spawns the process itself:

```json
{
  "mcpServers": {
    "email-marketing-tool": {
      "command": "uv",
      "args": ["run", "python", "-m", "app.mcp.server"],
      "cwd": "/path/to/Email_Marketing_Tool"
    }
  }
}
```

Verify it directly without a client:

```bash
uv run python -m app.mcp.server
```

should sit waiting for stdio input (Ctrl+C to exit) rather than erroring —
that's the same command Claude Desktop/Code run.

It currently serves `app.mcp.server.memory_service.InMemoryPlatformService`,
a seeded in-memory reference implementation (one demo campaign, lead, and
mailbox) — not the production database. See that module's docstring for
what a real, DB-backed implementation needs; dropping one in is a one-line
change in `app/mcp/server/app.py`, no tool or resource code changes.

For a remote client, run it over Streamable HTTP instead (needs `API_TOKEN`
set in `.env` — the same bearer token the REST API uses):

```python
import uvicorn
from app.core.config import Settings
from app.mcp.server.app import build_streamable_http_app
from app.mcp.server.memory_service import InMemoryPlatformService

app = build_streamable_http_app(InMemoryPlatformService(), Settings())
uvicorn.run(app, host="127.0.0.1", port=8000)
```

`launch_campaign` is the one tool that sends real, unrecoverable email — it
refuses to run without an explicit `confirm: true` argument, and its
description says so. Read the `docs://limits` resource before trusting any
claim a connected client makes about coverage or completeness.

## Project docs

`doc/` is gitignored (planning material, not shipped documentation) but if
you have it locally:

- `doc/00-OVERVIEW.md` — background and the measured funnel
- `doc/01-ARCHITECTURE.md` — system design
- `doc/02-ROADMAP.md` — session-by-session build order and progress
- `doc/03-TESTING.md` — testing strategy
