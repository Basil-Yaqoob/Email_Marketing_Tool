"""Google Gemini generateContent adapter.

The most divergent of the four: "contents" instead of "messages", "parts"
instead of content strings, "model" instead of "assistant" for the
assistant role, camelCase token counts, and the system prompt as
systemInstruction.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import httpx
from pydantic import BaseModel

from app.llm.providers.base import (
    DEFAULT_TIMEOUT,
    raise_for_status,
    refusal_guard,
    schema_of,
)
from app.llm.types import (
    FinishReason,
    Message,
    ProviderResponse,
    Role,
    Tool,
    ToolCall,
    Usage,
)

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

_FINISH_REASONS = {
    "STOP": FinishReason.STOP,
    "MAX_TOKENS": FinishReason.LENGTH,
    "SAFETY": FinishReason.CONTENT_FILTER,
    "RECITATION": FinishReason.CONTENT_FILTER,
}


def _strip_unsupported(schema: dict[str, Any]) -> dict[str, Any]:
    """Gemini's schema dialect rejects several JSON Schema keywords that
    Pydantic emits ($defs, additionalProperties, title). Dropping them is
    lossy for nested models, which is why the gateway validates the result
    with Pydantic regardless of what the provider claims to have enforced.
    """
    unsupported = {"$defs", "$schema", "additionalProperties", "title", "definitions"}
    cleaned = {k: v for k, v in schema.items() if k not in unsupported}
    if "properties" in cleaned and isinstance(cleaned["properties"], dict):
        cleaned["properties"] = {
            key: _strip_unsupported(value) if isinstance(value, dict) else value
            for key, value in cleaned["properties"].items()
        }
    if "items" in cleaned and isinstance(cleaned["items"], dict):
        cleaned["items"] = _strip_unsupported(cleaned["items"])
    return cleaned


class GeminiProvider:
    name = "gemini"

    def __init__(
        self,
        *,
        api_key: str,
        client: httpx.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        schema: type[BaseModel] | None = None,
        tools: Sequence[Tool] | None = None,
    ) -> ProviderResponse:
        contents: list[dict[str, Any]] = []
        system_parts: list[str] = []
        for message in messages:
            if message.role is Role.SYSTEM:
                system_parts.append(message.content)
                continue
            role = "model" if message.role is Role.ASSISTANT else "user"
            contents.append({"role": role, "parts": [{"text": message.content}]})

        payload: dict[str, Any] = {"contents": contents}
        if system_parts:
            payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
        if schema is not None:
            payload["generationConfig"] = {
                "responseMimeType": "application/json",
                "responseSchema": _strip_unsupported(schema_of(schema)),
            }
        if tools:
            payload["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": t.name,
                            "description": t.description,
                            "parameters": _strip_unsupported(dict(t.parameters)),
                        }
                        for t in tools
                    ]
                }
            ]

        response = await self._client.post(
            f"{BASE_URL}/models/{model}:generateContent",
            json=payload,
            headers={"x-goog-api-key": self._api_key, "content-type": "application/json"},
        )
        raise_for_status(response, provider=self.name, model=model)
        data = response.json()

        candidate = (data.get("candidates") or [{}])[0]
        parts = (candidate.get("content") or {}).get("parts") or []

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for part in parts:
            if "text" in part:
                text_parts.append(part["text"])
            elif "functionCall" in part:
                call = part["functionCall"]
                tool_calls.append(
                    ToolCall(
                        id=call.get("name", ""),
                        name=call.get("name", ""),
                        arguments=dict(call.get("args") or {}),
                    )
                )

        text = "".join(text_parts)
        finish_raw = candidate.get("finishReason") or "STOP"
        refusal_guard(
            text,
            "content_filter" if finish_raw in ("SAFETY", "RECITATION") else "",
            provider=self.name,
        )

        # camelCase, and completion tokens live under candidatesTokenCount.
        usage_raw = data.get("usageMetadata") or {}
        usage = Usage(
            prompt_tokens=int(usage_raw.get("promptTokenCount", 0)),
            completion_tokens=int(usage_raw.get("candidatesTokenCount", 0)),
        )

        return ProviderResponse(
            text=text,
            model=data.get("modelVersion", model),
            usage=usage,
            finish_reason=_FINISH_REASONS.get(finish_raw, FinishReason.STOP),
            tool_calls=tuple(tool_calls),
        )


__all__ = ["GeminiProvider", "json"]
