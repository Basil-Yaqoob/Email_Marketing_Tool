"""Anthropic Messages API adapter.

Three real differences from the OpenAI shape, all absorbed here:

  - the system prompt is a top-level field, not a message with role=system
  - token counts are input_tokens/output_tokens, not prompt/completion
  - structured output is expressed as a forced single tool, since the
    Messages API has no response_format
"""

from __future__ import annotations

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

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 4096

# The synthetic tool used to force schema-shaped output.
STRUCTURED_TOOL_NAME = "emit_result"

_STOP_REASONS = {
    "end_turn": FinishReason.STOP,
    "max_tokens": FinishReason.LENGTH,
    "tool_use": FinishReason.TOOL_CALLS,
    "stop_sequence": FinishReason.STOP,
    "refusal": FinishReason.CONTENT_FILTER,
}


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: str,
        client: httpx.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._max_tokens = max_tokens

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        schema: type[BaseModel] | None = None,
        tools: Sequence[Tool] | None = None,
    ) -> ProviderResponse:
        system_parts = [m.content for m in messages if m.role is Role.SYSTEM]
        conversation: list[dict[str, Any]] = []
        for message in messages:
            if message.role is Role.SYSTEM:
                continue
            if message.role is Role.TOOL:
                conversation.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": message.tool_call_id or "",
                                "content": message.content,
                            }
                        ],
                    }
                )
                continue
            conversation.append({"role": message.role.value, "content": message.content})

        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": self._max_tokens,
            "messages": conversation,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)

        declared_tools: list[dict[str, Any]] = [
            {"name": t.name, "description": t.description, "input_schema": dict(t.parameters)}
            for t in (tools or [])
        ]
        if schema is not None:
            declared_tools.append(
                {
                    "name": STRUCTURED_TOOL_NAME,
                    "description": "Return the result in the required structure.",
                    "input_schema": schema_of(schema),
                }
            )
            payload["tool_choice"] = {"type": "tool", "name": STRUCTURED_TOOL_NAME}
        if declared_tools:
            payload["tools"] = declared_tools

        response = await self._client.post(
            API_URL,
            json=payload,
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": API_VERSION,
                "content-type": "application/json",
            },
        )
        raise_for_status(response, provider=self.name, model=model)
        data = response.json()

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in data.get("content") or []:
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                arguments = block.get("input") or {}
                if block.get("name") == STRUCTURED_TOOL_NAME:
                    # The schema payload is the *answer*, not a tool the
                    # caller has to run — surface it as text so the gateway
                    # validates it the same way as every other provider.
                    import json as _json

                    text_parts.append(_json.dumps(arguments))
                else:
                    tool_calls.append(
                        ToolCall(
                            id=block.get("id", ""),
                            name=block.get("name", ""),
                            arguments=dict(arguments),
                        )
                    )

        text = "".join(text_parts)
        stop_reason = data.get("stop_reason") or "end_turn"
        refusal_guard(
            text, "content_filter" if stop_reason == "refusal" else "", provider=self.name
        )

        usage_raw = data.get("usage") or {}
        usage = Usage(
            prompt_tokens=int(usage_raw.get("input_tokens", 0)),
            completion_tokens=int(usage_raw.get("output_tokens", 0)),
        )

        return ProviderResponse(
            text=text,
            model=data.get("model", model),
            usage=usage,
            finish_reason=_STOP_REASONS.get(stop_reason, FinishReason.STOP),
            tool_calls=tuple(tool_calls),
        )
