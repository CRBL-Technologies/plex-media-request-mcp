"""One Messages API call to Claude."""

from __future__ import annotations

from typing import Any

import httpx

MESSAGES_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2026-10-21"
MODEL_TIMEOUT_SECONDS = 120
# The limit covers the model's thinking and its reply. A chat reply needs far less.
MAX_TOKENS = 8192
CACHED = {"type": "ephemeral"}


class ModelError(RuntimeError):
    pass


class Model:
    def __init__(self, api_key: str, model: str):
        self._headers = {"x-api-key": api_key, "anthropic-version": API_VERSION}
        self.model = model
        self._http = httpx.AsyncClient(timeout=MODEL_TIMEOUT_SECONDS)

    async def close(self) -> None:
        await self._http.aclose()

    async def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Return the content blocks of the reply: text, tool calls, or both."""

        body = {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "tools": tools,
            # Two cache points. This one holds the tools and the system prompt, which
            # every conversation shares.
            "system": [{"type": "text", "text": system, "cache_control": CACHED}],
            "messages": messages,
            # This one moves to the last message, so each round of a tool loop reads
            # the rounds before it from the cache.
            "cache_control": CACHED,
        }
        try:
            response = await self._http.post(MESSAGES_URL, headers=self._headers, json=body)
        except httpx.HTTPError as exc:
            raise ModelError(f"model request failed: {type(exc).__name__}") from None
        try:
            value = response.json()
        except ValueError:
            value = None
        if response.status_code >= 400 or not isinstance(value, dict):
            error = value.get("error") if isinstance(value, dict) else None
            kind = error.get("type") if isinstance(error, dict) else None
            raise ModelError(f"model request failed with HTTP {response.status_code} ({kind})")
        content = value.get("content")
        if not isinstance(content, list) or not all(isinstance(block, dict) for block in content):
            raise ModelError("model returned no message")
        # A reply cut off at the limit can end in half a tool call.
        if value.get("stop_reason") == "max_tokens":
            raise ModelError("model reply exceeded the token limit")
        return content


def tool_definition(schema: dict[str, Any]) -> dict[str, Any]:
    """Translate one gateway (MCP-style) tool schema into the Messages API form."""

    return {
        "name": schema["name"],
        "description": schema.get("description") or "",
        "input_schema": schema.get("inputSchema") or {"type": "object", "properties": {}},
    }
