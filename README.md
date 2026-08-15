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

## Project docs

`doc/` is gitignored (planning material, not shipped documentation) but if
you have it locally:

- `doc/00-OVERVIEW.md` — background and the measured funnel
- `doc/01-ARCHITECTURE.md` — system design
- `doc/02-ROADMAP.md` — session-by-session build order and progress
- `doc/03-TESTING.md` — testing strategy
