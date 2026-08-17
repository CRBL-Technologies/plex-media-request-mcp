from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from media_gateway import plex_watch
from media_gateway.config import Config
from media_gateway.password import hash_password


@pytest.fixture(autouse=True)
def _isolate_plex_slug_cache() -> Any:
    """Keep one test's resolved slug from answering another test's lookup."""

    plex_watch.clear_cache()
    yield
    plex_watch.clear_cache()


class FakeUpstream:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses: dict[str, Any] = {}
        self.tool_schemas: list[dict[str, Any]] = []

    async def list_tools(self) -> list[dict[str, Any]]:
        return self.tool_schemas

    async def call(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        value = self.responses.get(name, {})
        if callable(value):
            return value(arguments)
        return value

    async def radarr_queue(self, *, limit: int = 50) -> list[dict[str, Any]]:
        self.calls.append(("radarr_queue", {"limit": limit}))
        value = self.responses.get("radarr_queue", [])
        if callable(value):
            value = value({"limit": limit})
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, list) else []


@pytest.fixture
def config(tmp_path: Path) -> Config:
    policy = tmp_path / "hermes.env"
    policy.write_text(
        "TELEGRAM_BOT_TOKEN=test-token\nTELEGRAM_ALLOWED_USERS=1001\nTELEGRAM_ADMIN_USERS=9001\n",
        encoding="utf-8",
    )
    gateway = tmp_path / "gateway.key"
    gateway.write_text("gateway-secret-with-at-least-32-bytes\n", encoding="utf-8")
    upstream = tmp_path / "upstream.env"
    upstream.write_text("MCP_AUTH_TOKEN=upstream-secret-with-at-least-32-bytes\n", encoding="utf-8")
    password = tmp_path / "password.hash"
    password.write_text(hash_password("correct horse battery staple"), encoding="utf-8")
    session = tmp_path / "session.key"
    session.write_text("session-secret-with-enough-entropy\n", encoding="utf-8")
    webhook = tmp_path / "webhook.key"
    webhook.write_text("plex-hook-secret-with-at-least-32-bytes\n", encoding="utf-8")
    return Config(
        host="127.0.0.1",
        port=18082,
        db_path=tmp_path / "gateway.sqlite3",
        policy_file=policy,
        gateway_token_file=gateway,
        upstream_url="http://upstream:3000",
        upstream_token_file=upstream,
        dashboard_password_hash_file=password,
        dashboard_session_key_file=session,
        plex_webhook_token_file=webhook,
        secure_cookies=False,
        radarr_profile_id=10,
        radarr_root="/data/movies",
        radarr_tags=(3,),
        sonarr_profile_id=20,
        sonarr_anime_profile_id=21,
        sonarr_root="/data/tv",
        sonarr_tags=(4,),
        plex_machine_id="machine-123",
        notification_delay_seconds=1,
        telegram_identity_sync=False,
    )
