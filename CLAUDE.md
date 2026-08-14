# CLAUDE.md

Project context for Claude Code. Read this before doing anything in this repo.

---

## 1. What this project is

A **self-hosted, local-first cold email platform**. It replaces a rented stack
(Apollo + Clay + Instantly + a verification vendor, ~$500–1500/mo) with one
application the user runs themselves, using their own API keys.

The pipeline, end to end:

```
ICP definition → company discovery → company enrichment → person identification
→ email resolution + verification → signal/hook mining → copy generation
→ sending infrastructure → throttled sending → reply handling → analytics
```

**It runs on the user's own machine or their own VPS.** That is a deliberate
architectural choice, not a shortcut. It means: scraping happens from the user's
IP, SMTP verification uses a port-25-enabled host they control, LLM calls use
their keys (zero inference cost to us), and no shared-tenant deliverability
contamination.

### Prior art in this repo's history

Two working prototypes exist outside this repo and are the source material:

- `D:\Lead Generation` — Python pipeline: Google Places scrape → site crawl →
  email guess → verify → build list. Its verification stage is **broken** (the
  vendor account hit 0 credits; every call returns the string `"error"` and the
  run still exits 0).
- `D:\Leads Work` — `recent_news_agent` (hook mining) and `email_writer`
  (strategist/copywriter/critic with a deterministic quality gate). **The
  quality of this code is high and it is being ported, not replaced.**

Read `doc/00-OVERVIEW.md` for the full background and the measured funnel.

---

## 2. Non-negotiable rules

These exist because of specific failures observed in the prototypes. Do not
relax them.

### 2.1 Fail loud, never silently degrade
The prototype recorded 1,640 consecutive verification failures as the string
`"error"`, wrote a results file, printed a success summary, and exited 0.

- A stage that cannot do its job **raises**. It does not return a sentinel.
- Every batch operation tracks an error rate and **aborts past a threshold**
  (default 5%).
- Never write `except Exception:` followed by a return of a placeholder value.
  Catch narrow exceptions, or let it propagate.

### 2.2 Every fact carries provenance and confidence
No value enters the database as a bare string. Facts are stored as candidates
with `value`, `confidence` (0.0–1.0), `source` (which resolver), `source_url`,
and `retrieved_at`.

**An LLM never sees raw scraped HTML when writing copy.** It writes from a
structured fact sheet. A claim without a source URL is dropped, not softened.

### 2.3 Free before paid, always
Data sources are tiered `FREE` → `OWNED` → `METERED`. The waterfall executor
runs all free sources first and only escalates when confidence is below
threshold. A metered call that could have been avoided is a bug.

### 2.4 No secrets in source
The prototype hardcoded three live API keys as `os.getenv` defaults. Config
loading **raises** on a missing required key. There are no fallback values for
secrets, ever. Only `.env.example` is committed.

### 2.5 Deterministic checks are not delegated to the model
Anything mechanically checkable — em dashes, banned phrases, word counts,
missing unsubscribe headers, subject-line length — is enforced by code. The
model gets no vote. (Ported from `email_writer/quality.py`, which got this
right.)

### 2.6 Scraped content is untrusted input
Fetched pages and third-party fields are framed as evidence, never as
instructions. Prompt injection through a scraped page is a live risk in this
product category.

---

## 3. Tech stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.12 | All prior art is Python |
| API + UI | FastAPI + Jinja2 + HTMX | One language, one process, no node build step |
| DB | PostgreSQL 16 + SQLAlchemy 2.0 (async) | Concurrent workers need real transactions |
| Migrations | Alembic | — |
| Queue | Redis + `arq` | Async-native, far simpler than Celery |
| HTTP | `httpx` (async) | — |
| Parsing | `selectolax` (fast path), `BeautifulSoup` (fallback) | — |
| Browser | Playwright | JS-heavy pages; headed mode for user-owned sessions |
| LLM | OpenRouter default, provider adapters behind one interface | BYO key |
| Tests | `pytest`, `pytest-asyncio`, `respx`, `pytest-cov` | — |
| Lint/type | `ruff`, `mypy --strict` | — |
| Packaging | `uv`, Docker Compose | — |
| Logging | `structlog` (JSON) | Machine-readable telemetry |

**Do not add a dependency without noting why in the session plan.** No Next.js,
no Celery, no ORM other than SQLAlchemy, no pandas in application code (fine in
scripts).

---

## 4. Repo layout

```
app/
  core/          config, logging, errors, types
  db/            models, session, migrations/
  resolvers/     the waterfall — one module per data source
    base.py      Resolver protocol, Candidate, Tier
    executor.py  the waterfall engine
    discovery/   osm.py, google_places.py, ...
    company/     website.py, ...
    person/      registries.py, serp.py, impressum.py, ...
    email/       patterns.py, verify.py, ...
  net/           http client, cache, rate limiting, robots
  llm/           gateway, providers, cost tracking, prompts
  agents/        hook_miner.py, copywriter.py, quality.py
  sending/       mailboxes.py, engine.py, dns.py, threading.py
  replies/       imap.py, matcher.py, classifier.py
  policy/        jurisdiction.py, suppression.py, linter.py
  analytics/     health.py, seeds.py, yield_report.py
  api/           routes/
  web/           templates/, static/
  mcp/           client.py, server.py
  workers/       arq task definitions
tests/
  unit/  integration/  fixtures/
doc/             planning docs (GITIGNORED)
docker-compose.yml
.env.example
```

---

## 5. Commands

```bash
uv sync                          # install deps
docker compose up -d postgres redis
uv run alembic upgrade head      # migrate
uv run pytest                    # all tests
uv run pytest tests/unit -q      # fast loop
uv run pytest --cov=app --cov-report=term-missing
uv run ruff check . && uv run ruff format --check .
uv run mypy app
uv run uvicorn app.main:app --reload
uv run arq app.workers.WorkerSettings
```

`make check` runs lint + types + tests. **It must pass before any commit.**

---

## 6. Conventions

- **Async by default.** Any I/O is `async def`. No `requests`, use `httpx`.
- **Type everything.** `mypy --strict` passes. No bare `Any` without a comment.
- **Pydantic** for all boundary data (API, LLM output, config). SQLAlchemy
  models are for persistence only — do not pass them across layers.
- **No business logic in route handlers or in `__init__.py`.**
- **Structured logging.** `log.info("resolver.hit", resolver=..., lead_id=...)`,
  never f-string log messages.
- **Naming:** resolvers are `<Source>Resolver`; tasks are verbs
  (`discover_companies`); tables are plural snake_case.
- Comments explain *why*, not *what*. Match surrounding density.

---

## 7. Testing (required, per session)

Every session ships tests. See `doc/03-TESTING.md`.

- **Unit tests** — pure logic, no network, no DB. Fast.
- **Integration tests** — real Postgres and Redis via Docker Compose.
- **Network is always mocked** in tests using `respx`. A test that makes a real
  outbound request is a broken test. The only exception is an explicitly marked
  `@pytest.mark.live` suite, excluded from the default run.
- **Fixtures over mocks** for HTML: save real pages to
  `tests/fixtures/html/` and parse those.
- Coverage floor: **80%** on new code. `make check` enforces it.

A session is not done until `make check` is green.

---

## 8. Git

Work directly on `main` unless told otherwise (single-user project).

**Commit message format:**

```
<type>(<scope>): <summary>

<body: what changed and why, wrapped at 72 chars>
```

Types: `feat`, `fix`, `test`, `refactor`, `docs`, `chore`, `perf`.

> **IMPORTANT: Do NOT add a `Co-Authored-By: Claude` trailer to commits.**
> The user has explicitly asked for this to be omitted. No AI attribution
> footer, no "Generated with Claude Code" line. Plain commit messages only.

One commit per session, at the end, after `make check` passes. If a session
produced nothing committable, say so rather than committing a stub.

---

## 9. How sessions work

Build order and scope live in `doc/02-ROADMAP.md`. Each session has a detailed
plan in `doc/plans/session-NN-*.md` and a matching prompt in `doc/prompts.md`.

**At the start of a session:** read this file, read the session's plan, and
check `doc/02-ROADMAP.md` for what is already done.

**At the end of a session:** run `make check`, commit, then tick the session's
boxes in `doc/02-ROADMAP.md`.

Stay inside the session's scope. If you spot work that belongs to a later
session, note it in the roadmap's "Discovered work" section rather than doing
it. Scope creep is what makes a session unverifiable.

---

## 10. Known hard limits

State these plainly; do not let the product imply otherwise.

- Decision-maker email coverage tops out around **55–70%**, not 100%.
- SMTP verification is **blind against Google Workspace and Microsoft 365**
  hosted domains and against catch-alls. Those resolve to `unknown` and are
  handled by sending policy, not by better code.
- **Sending infrastructure costs money** (~$175–315/mo). BYO keys makes the
  intelligence nearly free; it cannot make mailboxes free.
- **No safe automated LinkedIn scraping exists.** Use SERP snippets
  (`site:linkedin.com/in "CEO" "Company"`) which never request a page from
  linkedin.com. Never build cookie-based LinkedIn scraping into the product.
- Transactional providers (SendGrid, Mailgun, Postmark, SES) **ban cold
  outreach** in their AUPs. Never integrate them as a sending path.
