"""One OpenAI-compatible chat-completions call through OpenRouter."""

from __future__ import annotations

from typing import Any

import httpx

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL_TIMEOUT_SECONDS = 120


class ModelError(RuntimeError):
    pass


class Model:
    def __init__(self, api_key: str, model: str):
        self._headers = {"Authorization": f"Bearer {api_key}", "X-Title": "CRBL media bot"}
        self.model = model
        self._http = httpx.AsyncClient(timeout=MODEL_TIMEOUT_SECONDS)

    async def close(self) -> None:
        await self._http.aclose()

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Return the assistant message: text, tool calls, or both."""

        body: dict[str, Any] = {"model": self.model, "messages": messages}
        if tools:
            body["tools"] = tools
        try:
            response = await self._http.post(OPENROUTER_URL, headers=self._headers, json=body)
        except httpx.HTTPError as exc:
            raise ModelError(f"model request failed: {type(exc).__name__}") from None
        try:
            value = response.json()
        except ValueError:
            value = None
        if response.status_code >= 400 or not isinstance(value, dict):
            raise ModelError(f"model request failed with HTTP {response.status_code}")
        choices = value.get("choices")
        message = choices[0].get("message") if isinstance(choices, list) and choices else None
        if not isinstance(message, dict):
            raise ModelError("model returned no message")
        return message


def function_tool(schema: dict[str, Any]) -> dict[str, Any]:
    """Translate one gateway (MCP-style) tool schema into the chat-completions form."""

    return {
        "type": "function",
        "function": {
            "name": schema["name"],
            "description": schema.get("description") or "",
            "parameters": schema.get("inputSchema") or {"type": "object", "properties": {}},
        },
    }
