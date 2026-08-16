"""The Provider protocol, and the shared HTTP/error handling every
adapter needs.

Everything that differs between providers — tool-call shapes, token field
names, how structured output is requested, where the refusal signal lives
— stays inside an adapter. The gateway above only ever sees a
ProviderResponse.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

import httpx
from pydantic import BaseModel

from app.core.errors import (
    ContentRefusalError,
    LLMError,
    ModelNotFoundError,
    RateLimitedError,
    UpstreamError,
)
from app.llm.types import Message, ProviderResponse, Tool

DEFAULT_TIMEOUT = 120.0

# What marks a 404 as "that model id doesn't exist" rather than a generic
# routing miss. OpenRouter retires ids regularly and the failure arrives as
# a plain 404, otherwise indistinguishable from a typo in the base URL.
#
# The discriminating token is "model" specifically. An earlier version also
# treated a bare "not found" as the signal, which matched almost any 404 --
# an nginx "not found" page would have sent someone off to edit their model
# routing config for no reason.
_MODEL_NOT_FOUND_TOKEN = "model"


class Provider(Protocol):
    name: str

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        schema: type[BaseModel] | None = None,
        tools: Sequence[Tool] | None = None,
    ) -> ProviderResponse: ...


def schema_of(schema: type[BaseModel]) -> dict[str, Any]:
    return schema.model_json_schema()


def raise_for_status(response: httpx.Response, *, provider: str, model: str) -> None:
    """Turn an HTTP failure into the narrowest exception that fits.

    The distinctions matter downstream: RateLimitedError and UpstreamError
    are retried with backoff, ModelNotFoundError and ContentRefusalError
    are not.
    """
    if response.status_code < 400:
        return

    body = response.text[:2000]

    if response.status_code == 429:
        raise RateLimitedError(f"{provider} rate limited for {model}: {body}")
    if response.status_code == 404 and _MODEL_NOT_FOUND_TOKEN in body.lower():
        raise ModelNotFoundError(
            f"{provider} does not know model {model!r} — model ids are "
            f"deprecated and renamed regularly; check your routing config. {body}"
        )
    if response.status_code >= 500:
        raise UpstreamError(f"{provider} returned {response.status_code} for {model}: {body}")
    raise LLMError(f"{provider} returned {response.status_code} for {model}: {body}")


def parse_json_arguments(raw: str | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def refusal_guard(text: str, finish_reason: str, *, provider: str) -> None:
    if finish_reason == "content_filter":
        raise ContentRefusalError(
            f"{provider} refused to answer (content filter). Not retried — the "
            f"same prompt will be refused again. Response: {text[:400]}"
        )
