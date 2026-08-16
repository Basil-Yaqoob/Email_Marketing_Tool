"""Which providers are actually available.

A provider whose key is absent is **not registered**. It does not fail at
call time and it does not appear as an option — it simply is not there,
and the UI can say which providers are configured. That is explicit
degradation (CLAUDE.md rule 2.4's sibling): no fallback values for
secrets, and no pretending a source exists when it doesn't.
"""

from __future__ import annotations

import httpx

from app.core.config import Settings
from app.llm.providers.anthropic import AnthropicProvider
from app.llm.providers.base import Provider
from app.llm.providers.gemini import GeminiProvider
from app.llm.providers.openai_compatible import openai, openrouter

# OpenRouter first: one key, many models, which is the right BYO default.
PROVIDER_PREFERENCE = ("openrouter", "anthropic", "openai", "gemini")


def build_providers(
    settings: Settings, *, client: httpx.AsyncClient | None = None
) -> dict[str, Provider]:
    providers: dict[str, Provider] = {}

    if settings.openrouter_api_key is not None:
        providers["openrouter"] = openrouter(settings.openrouter_api_key.get_secret_value(), client)
    if settings.anthropic_api_key is not None:
        providers["anthropic"] = AnthropicProvider(
            api_key=settings.anthropic_api_key.get_secret_value(), client=client
        )
    if settings.openai_api_key is not None:
        providers["openai"] = openai(settings.openai_api_key.get_secret_value(), client)
    if settings.google_api_key is not None:
        providers["gemini"] = GeminiProvider(
            api_key=settings.google_api_key.get_secret_value(), client=client
        )

    return providers


def preferred(providers: dict[str, Provider]) -> str | None:
    for name in PROVIDER_PREFERENCE:
        if name in providers:
            return name
    return next(iter(providers), None)
