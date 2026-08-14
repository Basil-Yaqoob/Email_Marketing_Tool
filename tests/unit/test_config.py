"""Regression tests for app/core/config.py.

Test 3 (test_no_secret_has_a_default_value) is the one that matters — it is
the guard against the prototype's exact bug: three live API keys shipped as
os.getenv defaults. See CLAUDE.md rule 2.4.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from pydantic_core import PydanticUndefined

from app.core.config import Settings
from app.core.errors import MissingConfigError


def test_settings_load_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: all required vars set, no .env file involved."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.database_url == "postgresql+asyncpg://u:p@localhost/db"
    assert settings.redis_url == "redis://localhost:6379/0"
    assert settings.secret_key.get_secret_value() == "test-secret-key"
    # Tunables fall back to their safe, non-secret defaults.
    assert settings.error_rate_threshold == 0.05
    assert settings.confidence_threshold == 0.85


def test_missing_database_url_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A required field with no value raises ValidationError, not a default."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)  # type: ignore[call-arg]

    assert "database_url" in str(exc_info.value).lower()


def test_no_secret_has_a_default_value() -> None:
    """Regression guard: the prototype shipped three live keys as defaults.

    Introspects every SecretStr-typed field on Settings and asserts none has
    a non-None default. Write this so it fails the moment anyone adds
    `openrouter_api_key: SecretStr = "sk-..."`.
    """
    for name, field in Settings.model_fields.items():
        annotation_str = str(field.annotation)
        if "SecretStr" in annotation_str:
            assert field.default in (None, PydanticUndefined), (
                f"Settings.{name} has a hardcoded default. Secrets must never "
                f"have fallback values — see CLAUDE.md rule 2.4."
            )


def test_require_raises_clear_error_for_unset_optional_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """require() names the exact missing key so a resolver's error is legible."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    with pytest.raises(MissingConfigError) as exc_info:
        settings.require("openrouter_api_key")

    assert exc_info.value.key == "openrouter_api_key"
    assert "openrouter_api_key" in str(exc_info.value)


def test_require_returns_value_for_set_optional_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.require("openrouter_api_key").get_secret_value() == "sk-or-test"


def test_error_rate_threshold_rejects_out_of_range(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pydantic's ge=0.0, le=1.0 bounds actually apply."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("ERROR_RATE_THRESHOLD", "1.5")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]
