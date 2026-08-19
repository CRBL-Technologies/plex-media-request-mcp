"""Tiny authenticated client for the private media gateway."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx

from media_gateway.secrets import read_secret
from media_gateway.types import Actor, Role

CALL_TIMEOUT_SECONDS = 45
# request_titles walks up to a hundred titles four at a time, and each costs a
# lookup plus a library read plus the request itself. At 45 seconds the model
# is told the run failed while the gateway is still working through the list
# and will finish it -- and the obvious next move, retrying, requests
# everything twice.
BULK_CALL_TIMEOUT_SECONDS = 300
SLOW_TOOLS = frozenset({"request_titles"})


class GatewayError(RuntimeError):
    pass


class GatewayClient:
    def __init__(self, url: str, token_file: Path):
        self.url = url.rstrip("/")
        self.token_file = token_file

    @classmethod
    def from_env(cls) -> GatewayClient:
        return cls(
            os.getenv("CRBL_MEDIA_GATEWAY_URL", "http://media-gateway:18082"),
            Path(os.getenv("CRBL_MEDIA_GATEWAY_TOKEN_FILE", "/run/media-secrets/gateway.key")),
        )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {read_secret(self.token_file)}"}

    def schemas(self) -> list[dict[str, Any]]:
        with httpx.Client(timeout=30) as client:
            response = client.get(f"{self.url}/api/schema", headers=self._headers())
        response.raise_for_status()
        try:
            value = response.json().get("tools")
        except (json.JSONDecodeError, AttributeError) as exc:
            raise GatewayError("gateway returned an invalid tool schema") from exc
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise GatewayError("gateway returned an invalid tool schema")
        return value

    async def observe(self, actor: Actor, *, blocked: bool = False) -> Role:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{self.url}/api/actors",
                headers=self._headers(),
                json={"actor": self._actor(actor), "blocked": blocked},
            )
        response.raise_for_status()
        try:
            return Role(response.json()["role"])
        except (KeyError, ValueError, TypeError) as exc:
            raise GatewayError("gateway returned an invalid actor role") from exc

    async def call(self, actor: Actor, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        timeout = BULK_CALL_TIMEOUT_SECONDS if name in SLOW_TOOLS else CALL_TIMEOUT_SECONDS
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{self.url}/api/tools/call",
                headers=self._headers(),
                json={"actor": self._actor(actor), "name": name, "arguments": arguments},
            )
        try:
            value = response.json()
        except json.JSONDecodeError as exc:
            raise GatewayError("media gateway is unavailable") from exc
        if response.status_code >= 400 or not value.get("ok"):
            raise GatewayError(str(value.get("error") or "media operation failed"))
        result = value.get("result")
        if not isinstance(result, dict):
            raise GatewayError("gateway returned an invalid tool result")
        return result

    @staticmethod
    def _actor(actor: Actor) -> dict[str, object]:
        return {
            "user_id": actor.user_id,
            "chat_id": actor.chat_id,
            "username": actor.username,
            "first_name": actor.first_name,
            "last_name": actor.last_name,
        }
