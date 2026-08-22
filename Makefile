.PHONY: check lint types test test-fast live run run-mcp

check: lint types test

run:
	docker compose up -d postgres redis
	uv run alembic upgrade head
	uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

run-mcp:
	uv run python -m app.mcp.server

lint:
	uv run ruff check .
	uv run ruff format --check .

types:
	uv run mypy app

test:
	uv run pytest --cov=app --cov-report=term-missing --cov-fail-under=80

test-fast:
	uv run pytest tests/unit -q

live:
	uv run pytest -m live
