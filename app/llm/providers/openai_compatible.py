"""OpenAI-compatible chat-completions adapter.

OpenRouter and OpenAI speak the same wire format, so they share one
implementation parameterised by base URL and auth headers. Keeping them as
one adapter rather than two near-copies means a bug in tool-call parsing
gets fixed once.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import httpx
from pydantic import BaseModel

from app.llm.providers.base import (
    DEFAULT_TIMEOUT,
    parse_json_arguments,
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

_FINISH_REASONS = {
    "stop": FinishReason.STOP,
    "length": FinishReason.LENGTH,
    "tool_calls": FinishReason.TOOL_CALLS,
    "content_filter": FinishReason.CONTENT_FILTER,
}


def _message_payload(message: Message) -> dict[str, Any]:
    if message.role is Role.TOOL:
        return {
            "role": "tool",
            "content": message.content,
            "tool_call_id": message.tool_call_id,
        }
    return {"role": message.role.value, "content": message.content}


class OpenAICompatibleProvider:
    def __init__(
        self,
        *,
        name: str,
        api_key: str,
        base_url: str,
        client: httpx.AsyncClient | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.name = name
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._extra_headers = extra_headers or {}

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        schema: type[BaseModel] | None = None,
        tools: Sequence[Tool] | None = None,
    ) -> ProviderResponse:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [_message_payload(m) for m in messages],
        }
        if schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "strict": True,
                    "schema": schema_of(schema),
                },
            }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": dict(t.parameters),
                    },
                }
                for t in tools
            ]

        response = await self._client.post(
            f"{self._base_url}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {self._api_key}", **self._extra_headers},
        )
        raise_for_status(response, provider=self.name, model=model)
        data = response.json()

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = message.get("content") or ""
        finish_raw = choice.get("finish_reason") or "stop"
        refusal_guard(text, finish_raw, provider=self.name)

        tool_calls = tuple(
            ToolCall(
                id=call.get("id", ""),
                name=call.get("function", {}).get("name", ""),
                arguments=parse_json_arguments(call.get("function", {}).get("arguments", "{}")),
            )
            for call in (message.get("tool_calls") or [])
        )

        # OpenAI-family field names. Normalised here so the gateway never
        # sees a provider-specific shape.
        usage_raw = data.get("usage") or {}
        usage = Usage(
            prompt_tokens=int(usage_raw.get("prompt_tokens", 0)),
            completion_tokens=int(usage_raw.get("completion_tokens", 0)),
        )

        return ProviderResponse(
            text=text,
            model=data.get("model", model),
            usage=usage,
            finish_reason=_FINISH_REASONS.get(finish_raw, FinishReason.STOP),
            tool_calls=tool_calls,
        )


def openrouter(api_key: str, client: httpx.AsyncClient | None = None) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        name="openrouter",
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
        client=client,
        # OpenRouter asks integrators to identify themselves; it also
        # affects rate-limit tiering.
        extra_headers={"HTTP-Referer": "https://github.com/local/email-marketing-tool"},
    )


def openai(api_key: str, client: httpx.AsyncClient | None = None) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        name="openai",
        api_key=api_key,
        base_url="https://api.openai.com/v1",
        client=client,
    )
