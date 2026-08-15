"""Official MCP client for the immutable upstream service."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from .secrets import read_dotenv, read_secret


class UpstreamError(RuntimeError):
    pass


class Upstream:
    def __init__(self, url: str, token_file: Path):
        self.url = f"{url.rstrip('/')}/mcp"
        self.token_file = token_file

    def _token(self) -> str:
        if self.token_file.suffix == ".env":
            values = read_dotenv(self.token_file, {"MCP_AUTH_TOKEN"})
            token = values.get("MCP_AUTH_TOKEN", "")
            if len(token) < 32:
                raise UpstreamError("upstream token is missing or too short")
            return token
        return read_secret(self.token_file, minimum=32)

    async def list_tools(self) -> list[dict[str, Any]]:
        try:
            async with (
                streamablehttp_client(
                    self.url, headers={"Authorization": f"Bearer {self._token()}"}, timeout=20
                ) as (reader, writer, _),
                ClientSession(reader, writer) as session,
            ):
                await session.initialize()
                response = await session.list_tools()
                return [
                    {
                        "name": tool.name,
                        "description": tool.description or "",
                        "inputSchema": tool.inputSchema,
                    }
                    for tool in response.tools
                ]
        except UpstreamError:
            raise
        except Exception as exc:
            raise UpstreamError("upstream MCP connection failed") from exc

    async def call(self, name: str, arguments: dict[str, Any]) -> Any:
        try:
            async with (
                streamablehttp_client(
                    self.url, headers={"Authorization": f"Bearer {self._token()}"}, timeout=30
                ) as (reader, writer, _),
                ClientSession(reader, writer) as session,
            ):
                await session.initialize()
                result = await session.call_tool(name, arguments)
        except UpstreamError:
            raise
        except Exception as exc:
            raise UpstreamError("upstream MCP connection failed") from exc
        if result.isError:
            message = "upstream tool failed"
            for content in result.content:
                text = getattr(content, "text", None)
                if isinstance(text, str) and text:
                    message = text[:500]
                    break
            raise UpstreamError(message)
        structured = getattr(result, "structuredContent", None)
        if structured is not None:
            return structured
        for content in result.content:
            text = getattr(content, "text", None)
            if not isinstance(text, str):
                continue
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"message": text[:2000]}
        return {}

    async def radarr_queue(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Read the Radarr queue missing from upstream MCP 2.3.0.

        The pinned upstream advertises ``radarr_get_queue`` in its full tool
        profile but does not register an implementation. Keep this narrow and
        read-only until upstream provides the tool.
        """

        values = read_dotenv(self.token_file, {"RADARR_URL", "RADARR_API_KEY"})
        base = values.get("RADARR_URL", "").rstrip("/")
        api_key = values.get("RADARR_API_KEY", "")
        parsed = urlsplit(base)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or len(api_key) < 16
        ):
            raise UpstreamError("Radarr queue configuration is invalid")
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(
                    f"{base}/api/v3/queue",
                    headers={"X-Api-Key": api_key},
                    params={
                        "page": 1,
                        "pageSize": min(max(limit, 1), 100),
                        "includeUnknownMovieItems": "true",
                    },
                )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            raise UpstreamError("Radarr queue is unavailable") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
            raise UpstreamError("Radarr queue returned an invalid response")
        return [item for item in payload["records"] if isinstance(item, dict)]
