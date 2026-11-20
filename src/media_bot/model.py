"""One Messages API call to Claude."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

MESSAGES_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2026-10-21"
MODEL_TIMEOUT_SECONDS = 120
# The limit covers the model's thinking and its reply. A chat reply needs far less.
MAX_TOKENS = 8192
CACHED = {"type": "ephemeral"}
# An overload or a rate limit usually clears within seconds, so one more try
# hides most of them. A wait longer than this is not worth holding the chat for.
RETRY_DELAY_SECONDS = 2
RETRY_AFTER_LIMIT_SECONDS = 10
RETRY_STATUSES = frozenset({408, 409, 429})


class ModelError(RuntimeError):
    pass


class ModelBusy(ModelError):
    """A failure that a second try may clear. ``delay`` is how long to wait first."""

    def __init__(self, message: str, delay: float):
        super().__init__(message)
        self.delay = delay


class ModelCutOff(ModelError):
    """The reply reached the token limit before it was finished."""


class ModelRefused(ModelError):
    """The model declined to answer."""


def _retry_delay(response: httpx.Response, error: object) -> float | None:
    """How long to wait before one more try, or None when a retry cannot help."""

    advice = response.headers.get("x-should-retry")
    retryable = response.status_code in RETRY_STATUSES or response.status_code >= 500
    if advice == "false" or (advice != "true" and not retryable):
        return None
    details = error.get("details") if isinstance(error, dict) else None
    # A spent monthly limit answers 429 too, and stays spent.
    if isinstance(details, dict) and details.get("error_code") == "enforced_spend_limit_reached":
        return None
    try:
        delay = float(response.headers.get("retry-after", RETRY_DELAY_SECONDS))
    except ValueError:
        delay = RETRY_DELAY_SECONDS
    return delay if 0 <= delay <= RETRY_AFTER_LIMIT_SECONDS else None


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
            return await self._request(body)
        except ModelBusy as busy:
            await asyncio.sleep(busy.delay)
            return await self._request(body)

    async def _request(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            response = await self._http.post(MESSAGES_URL, headers=self._headers, json=body)
        except httpx.TimeoutException as exc:
            # The request already waited its full time. A second wait would outlast the turn.
            raise ModelError(f"model request failed: {type(exc).__name__}") from None
        except httpx.HTTPError as exc:
            message = f"model request failed: {type(exc).__name__}"
            raise ModelBusy(message, RETRY_DELAY_SECONDS) from None
        try:
            value = response.json()
        except ValueError:
            value = None
        if response.status_code >= 400 or not isinstance(value, dict):
            error = value.get("error") if isinstance(value, dict) else None
            kind = error.get("type") if isinstance(error, dict) else None
            message = f"model request failed with HTTP {response.status_code} ({kind})"
            delay = _retry_delay(response, error)
            if delay is None:
                raise ModelError(message)
            raise ModelBusy(message, delay)
        # A refusal can arrive with no content at all, so read the reason first.
        if value.get("stop_reason") == "refusal":
            raise ModelRefused("model declined the request")
        content = value.get("content")
        if not isinstance(content, list) or not all(isinstance(block, dict) for block in content):
            raise ModelError("model returned no message")
        # A reply cut off at the limit can end in half a tool call.
        if value.get("stop_reason") == "max_tokens":
            raise ModelCutOff("model reply exceeded the token limit")
        return content


def tool_definition(schema: dict[str, Any]) -> dict[str, Any]:
    """Translate one gateway (MCP-style) tool schema into the Messages API form."""

    return {
        "name": schema["name"],
        "description": schema.get("description") or "",
        "input_schema": schema.get("inputSchema") or {"type": "object", "properties": {}},
    }
