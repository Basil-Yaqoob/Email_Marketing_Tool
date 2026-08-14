.PHONY: check lint types test test-fast live

check: lint types test

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
