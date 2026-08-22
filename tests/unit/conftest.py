"""Unit-test isolation from the developer's real .env.

`Settings` reads `.env` at construction. A unit test that builds one
therefore inherits whatever happens to be configured on the machine
running it, which makes "no key configured" mean "no key configured
*here*" -- and that is not a property of the code under test.

This has bitten twice, both times as a test that passed on a bare machine
and failed the moment real config existed:

  - test_unconfigured_provider_not_registered asserted no LLM providers
    are built with no keys set. It passed until OPENROUTER_API_KEY was
    added to .env.
  - test_build_search_backend_raises_without_any_configured asserted
    build_search_backend raises with no backend configured. It passed
    until SEARXNG_URL was added.

Both are real assertions about real behaviour; both were silently
depending on the absence of a file they never mentioned. This fixture
points `env_file` at nothing for every unit test, so a unit test sees only
the settings it passes explicitly.

Integration tests deliberately do the opposite -- they read the real .env
for DATABASE_URL -- so this lives in tests/unit/, not tests/.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.core.config import Settings


@pytest.fixture(autouse=True)
def _isolate_settings_from_dotenv() -> Iterator[None]:
    """Make Settings() ignore .env for the duration of every unit test."""
    original = Settings.model_config.get("env_file")
    Settings.model_config["env_file"] = None
    try:
        yield
    finally:
        Settings.model_config["env_file"] = original
