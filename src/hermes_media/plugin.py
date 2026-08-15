"""Minimal Hermes v2026.8.3 Telegram adapter and role-aware tools."""

from __future__ import annotations

import inspect
import json
import os
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from media_gateway.constants import ADMIN_UPSTREAM_TOOLS, SHARED_TOOLS
from media_gateway.types import Role

from .client import GatewayClient
from .trusted import actor_from_event, actor_scope, current_role, require_actor

PLATFORM = "telegram"
SHARED_TOOLSET = "crbl-media-shared"
ADMIN_TOOLSET = "crbl-media-admin"
NATIVE_MODULE = "plugins.platforms.telegram.adapter"
NATIVE_CLASS = "TelegramAdapter"


class _FallbackAdapter:
    def __init__(self, config: object) -> None:
        self.config = config

    async def handle_message(self, event: object) -> object:
        return event


def _native_adapter() -> type:
    try:
        module = __import__(NATIVE_MODULE, fromlist=[NATIVE_CLASS])
        candidate = getattr(module, NATIVE_CLASS)
        return candidate if inspect.isclass(candidate) else _FallbackAdapter
    except (ImportError, AttributeError):
        return _FallbackAdapter


_NativeAdapter = _native_adapter()
_client: GatewayClient | None = None


def _gateway() -> GatewayClient:
    global _client
    if _client is None:
        _client = GatewayClient.from_env()
    return _client


class MediaTelegramAdapter(_NativeAdapter):  # type: ignore[misc, valid-type]
    @property
    def authorization_is_upstream(self) -> bool:
        """Report the gateway's authenticated actor decision to Hermes."""

        return True

    async def handle_message(self, event: object) -> object:
        actor = actor_from_event(event)
        role = await _gateway().observe(actor)
        if role is Role.BLOCKED:
            raise PermissionError("Telegram user is not allowed")
        with actor_scope(actor, role):
            result = super().handle_message(event)
            return await result if inspect.isawaitable(result) else result


def _visibility_patch() -> None:
    try:
        from hermes_cli import tools_config  # type: ignore[import-not-found]
    except ImportError:
        return
    original = getattr(tools_config, "_get_platform_tools", None)
    if not callable(original) or getattr(original, "__crbl_media__", False):
        return

    def visible(config: dict[str, Any], platform: str, *args: Any, **kwargs: Any) -> object:
        resolved = original(config, platform, *args, **kwargs)
        if str(platform).lower() != PLATFORM:
            return resolved
        toolsets = {SHARED_TOOLSET}
        if current_role() is Role.ADMIN:
            toolsets.add(ADMIN_TOOLSET)
        return toolsets

    visible.__crbl_media__ = True  # type: ignore[attr-defined]
    tools_config._get_platform_tools = visible


def _handler(name: str) -> Callable[..., Awaitable[str]]:
    async def call(arguments: Mapping[str, Any], **runtime: Any) -> str:
        del runtime
        result = await _gateway().call(require_actor(), name, dict(arguments))
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))

    return call


def _adapter(config: object) -> MediaTelegramAdapter:
    return MediaTelegramAdapter(config)


def _configured(config: object) -> bool:
    token = getattr(config, "token", None) or os.getenv("TELEGRAM_BOT_TOKEN")
    return isinstance(token, str) and bool(token.strip())


def register(ctx: object) -> None:
    _visibility_patch()
    register_platform = getattr(ctx, "register_platform", None)
    register_tool = getattr(ctx, "register_tool", None)
    if not callable(register_platform) or not callable(register_tool):
        raise TypeError("Hermes plugin API is unavailable")
    register_platform(
        name=PLATFORM,
        label="Telegram (CRBL media policy)",
        adapter_factory=_adapter,
        check_fn=lambda: _native_adapter() is not _FallbackAdapter,
        validate_config=_configured,
        is_connected=_configured,
        required_env=["TELEGRAM_BOT_TOKEN"],
        allowed_users_env="TELEGRAM_ALLOWED_USERS",
        # If this plugin fails to register, the native Telegram adapter and
        # its ordinary TELEGRAM_ALLOWED_USERS gate remain in force.
        allow_all_env="TELEGRAM_ALLOW_ALL_USERS",
        plugin_name="crbl-media",
        emoji="✈️",
        max_message_length=4096,
        allow_update_command=True,
        platform_hint="Telegram identity is trusted automatically; never request user IDs.",
    )
    schemas = _gateway().schemas()
    names: set[str] = set()
    shared_names: set[str] = set()
    admin_names: set[str] = set()
    for item in schemas:
        name = item.get("name")
        description = item.get("description")
        parameters = item.get("inputSchema")
        scope = item.get("scope")
        if (
            not isinstance(name, str)
            or name in names
            or not isinstance(description, str)
            or not isinstance(parameters, dict)
            or scope not in {"shared", "admin"}
        ):
            raise RuntimeError("gateway tool contract is invalid")
        if (name in SHARED_TOOLS) != (scope == "shared"):
            raise RuntimeError("gateway tool scope is invalid")
        names.add(name)
        (shared_names if scope == "shared" else admin_names).add(name)
        register_tool(
            name=name,
            toolset=SHARED_TOOLSET if scope == "shared" else ADMIN_TOOLSET,
            schema={"name": name, "description": description, "parameters": parameters},
            handler=_handler(name),
            is_async=True,
            description=description,
            emoji="🎬",
        )
    if shared_names != set(SHARED_TOOLS) or admin_names != ADMIN_UPSTREAM_TOOLS:
        raise RuntimeError("gateway tool inventory is incomplete")
