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

    def _arr(self, service: str) -> tuple[str, str]:
        """Base URL and API key for Radarr or Sonarr from the upstream secrets."""

        prefix = {"radarr": "RADARR", "sonarr": "SONARR"}[service]
        values = read_dotenv(self.token_file, {f"{prefix}_URL", f"{prefix}_API_KEY"})
        base = values.get(f"{prefix}_URL", "").rstrip("/")
        api_key = values.get(f"{prefix}_API_KEY", "")
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
            raise UpstreamError(f"{service.capitalize()} configuration is invalid")
        return base, api_key

    async def ensure_tag(self, service: str, label: str) -> int:
        """Return the ID of a Radarr/Sonarr tag, creating it when missing.

        Upstream MCP 2.3.0 has no tag tools, and a tag must exist before an
        item can carry it. Both apps compare labels case-insensitively and
        reject duplicates, so look up before creating.
        """

        base, api_key = self._arr(service)
        headers = {"X-Api-Key": api_key}
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(f"{base}/api/v3/tag", headers=headers)
                response.raise_for_status()
                tags = response.json()
                if not isinstance(tags, list):
                    raise ValueError("invalid tag list")
                for tag in tags:
                    if (
                        isinstance(tag, dict)
                        and isinstance(tag.get("label"), str)
                        and tag["label"].casefold() == label.casefold()
                        and isinstance(tag.get("id"), int)
                    ):
                        return int(tag["id"])
                response = await client.post(
                    f"{base}/api/v3/tag", headers=headers, json={"label": label}
                )
                response.raise_for_status()
                created = response.json()
        except Exception as exc:
            raise UpstreamError(f"{service.capitalize()} tags are unavailable") from exc
        if not isinstance(created, dict) or not isinstance(created.get("id"), int):
            raise UpstreamError(f"{service.capitalize()} returned an invalid tag")
        return int(created["id"])

    async def add_tags(self, service: str, item_id: int, tag_ids: list[int]) -> None:
        """Add tags to one tracked movie or series, keeping the tags it has."""

        base, api_key = self._arr(service)
        path, key = {
            "radarr": ("movie/editor", "movieIds"),
            "sonarr": ("series/editor", "seriesIds"),
        }[service]
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.put(
                    f"{base}/api/v3/{path}",
                    headers={"X-Api-Key": api_key},
                    json={key: [item_id], "tags": tag_ids, "applyTags": "add"},
                )
            response.raise_for_status()
        except Exception as exc:
            raise UpstreamError(f"{service.capitalize()} tags could not be applied") from exc

    async def radarr_queue(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Read the Radarr queue missing from upstream MCP 2.3.0.

        The pinned upstream advertises ``radarr_get_queue`` in its full tool
        profile but does not register an implementation. Keep this narrow and
        read-only until upstream provides the tool.
        """

        try:
            base, api_key = self._arr("radarr")
        except UpstreamError:
            raise UpstreamError("Radarr queue configuration is invalid") from None
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

    async def plex_episodes(self, rating_key: str) -> list[dict[str, Any]]:
        """Read actual files under a show/season; container existence is not availability."""

        if not rating_key.isdecimal():
            raise UpstreamError("Plex metadata key is invalid")
        values = read_dotenv(self.token_file, {"PLEX_URL", "PLEX_API_KEY"})
        base = values.get("PLEX_URL", "").rstrip("/")
        token = values.get("PLEX_API_KEY", "")
        if not base or not token:
            raise UpstreamError("Plex configuration is unavailable")
        items: list[dict[str, Any]] = []
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                while True:
                    response = await client.get(
                        f"{base}/library/metadata/{rating_key}/allLeaves",
                        headers={"X-Plex-Token": token, "Accept": "application/json"},
                        params={"X-Plex-Container-Start": len(items), "X-Plex-Container-Size": 500},
                    )
                    response.raise_for_status()
                    container = response.json()["MediaContainer"]
                    rows = container.get("Metadata", [])
                    if not isinstance(rows, list) or any(not isinstance(x, dict) for x in rows):
                        raise ValueError("invalid episode list")
                    items.extend(rows)
                    if len(items) >= int(container.get("totalSize", len(items))):
                        return items
                    if not rows:
                        raise ValueError("incomplete episode list")
        except Exception:
            raise UpstreamError("Plex episodes are unavailable") from None
