"""Application settings.

CLAUDE.md rule 2.4: no secrets in source. Every required field below has no
default — Pydantic Settings raises ValidationError at import time if it is
missing from the environment or .env file. Optional keys default to None and
the features that need them degrade explicitly (see `require()`), never with
a hardcoded fallback string.
"""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.errors import MissingConfigError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Required. No defaults. Missing -> ValidationError at startup. -------
    database_url: str
    redis_url: str
    secret_key: SecretStr  # encrypts stored mailbox credentials

    # --- Optional: features degrade explicitly when absent -------------------
    openrouter_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    google_api_key: SecretStr | None = None
    google_maps_api_key: SecretStr | None = None
    companies_house_api_key: SecretStr | None = None
    opencorporates_api_key: SecretStr | None = None

    # --- Tunables with safe defaults ------------------------------------------
    error_rate_threshold: float = Field(0.05, ge=0.0, le=1.0)
    confidence_threshold: float = Field(0.85, ge=0.0, le=1.0)
    http_cache_dir: str = ".http_cache"
    log_level: str = "INFO"

    def require(self, name: str) -> SecretStr:
        """Fetch an optional key, raising a clear error if it wasn't set.

        Use this at the point a resolver or LLM adapter actually needs the
        key, so the error names exactly which feature is unavailable rather
        than failing generically or silently skipping the resolver.
        """
        value = getattr(self, name, None)
        if value is None:
            raise MissingConfigError(name)
        return value  # type: ignore[no-any-return]
