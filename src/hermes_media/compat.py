"""All compatibility code for the pinned Hermes v2026.8.3 runtime."""

from __future__ import annotations

import html
import importlib.metadata
import inspect
from collections.abc import Callable, Mapping
from types import SimpleNamespace
from typing import Any

PINNED_HERMES_RELEASE = "v2026.8.3"
PINNED_HERMES_PACKAGE_VERSION = "0.20.0"
NATIVE_MODULE = "plugins.platforms.telegram.adapter"
NATIVE_CLASS = "TelegramAdapter"


def native_adapter(fallback: type) -> type:
    try:
        module = __import__(NATIVE_MODULE, fromlist=[NATIVE_CLASS])
        candidate = getattr(module, NATIVE_CLASS)
        return candidate if inspect.isclass(candidate) else fallback
    except (ImportError, AttributeError):
        return fallback


def discard_native_clarify(adapter: object | None, clarify_id: str) -> None:
    """Remove one picker from Hermes' private Telegram clarify registry."""

    state = getattr(adapter, "_clarify_state", None)
    if isinstance(state, dict):
        state.pop(clarify_id, None)


def install_tool_visibility(
    *,
    current_role: Callable[[], object],
    admin_role: object,
    shared_toolset: str,
    admin_toolset: str,
    search_toolset: str,
) -> None:
    """Quarantine Hermes' private role-aware tool resolver patch."""

    try:
        from hermes_cli import tools_config  # type: ignore[import-not-found]
    except ImportError:
        return
    original = getattr(tools_config, "_get_platform_tools", None)
    if not callable(original) or getattr(original, "__crbl_media__", False):
        return

    def visible(config: dict[str, Any], platform: str, *args: Any, **kwargs: Any) -> object:
        resolved = original(config, platform, *args, **kwargs)
        if str(platform).lower() != "telegram":
            return resolved
        toolsets = {shared_toolset, search_toolset}
        if current_role() is admin_role:
            toolsets.add(admin_toolset)
        return toolsets

    visible.__crbl_media__ = True  # type: ignore[attr-defined]
    tools_config._get_platform_tools = visible


async def send_numbered_picker(
    adapter: object,
    *,
    chat_id: str,
    question: str,
    choices: list[str],
    clarify_id: str,
    session_key: str,
) -> object:
    """Render native-compatible numbered buttons without Hermes' free-text button."""

    bot = getattr(adapter, "_bot", None)
    clarify_state = getattr(adapter, "_clarify_state", None)
    if bot is None or not isinstance(clarify_state, dict):
        fallback = getattr(adapter, "send_clarify", None)
        if not callable(fallback):
            return SimpleNamespace(success=False)
        return await fallback(
            chat_id=chat_id,
            question=question,
            choices=choices,
            clarify_id=clarify_id,
            session_key=session_key,
            metadata=None,
        )

    from telegram import (  # type: ignore[import-not-found]
        InlineKeyboardButton,
        InlineKeyboardMarkup,
    )
    from telegram.constants import ParseMode  # type: ignore[import-not-found]

    option_lines = "\n".join(
        f"{index}. {html.escape(str(choice))}" for index, choice in enumerate(choices, 1)
    )
    text = f"❓ {html.escape(question)}\n\n{option_lines}"
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(str(index), callback_data=f"cl:{clarify_id}:{index - 1}")]
            for index in range(1, len(choices) + 1)
        ]
    )
    message = await bot.send_message(
        chat_id=int(chat_id),
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )
    clarify_state[clarify_id] = session_key
    return SimpleNamespace(success=True, message_id=str(message.message_id))


def verify_pinned_runtime(
    *, manager: object, config: Mapping[str, Any], expected_tools: set[str], platform_hint: str
) -> None:
    """Fail closed when any private pinned-Hermes integration contract moves."""

    from agent.prompt_builder import PLATFORM_HINTS  # type: ignore[import-not-found]
    from agent.system_prompt import _resolve_platform_hint  # type: ignore[import-not-found]
    from agent.web_search_registry import (  # type: ignore[import-not-found]
        get_active_extract_provider,
        get_active_search_provider,
    )
    from gateway.platform_registry import platform_registry  # type: ignore[import-not-found]
    from hermes_cli import tools_config
    from toolsets import TOOLSETS  # type: ignore[import-not-found]

    if importlib.metadata.version("hermes-agent") != PINNED_HERMES_PACKAGE_VERSION:
        raise RuntimeError("pinned Hermes package version changed")
    provider = get_active_search_provider()
    if provider is None or provider.name != "ddgs" or not provider.is_available():
        raise RuntimeError("DuckDuckGo search provider is unavailable")
    if get_active_extract_provider() is not None:
        raise RuntimeError("CRBL media bot must not enable web extraction")
    entry = platform_registry.get("telegram")
    if entry is None or entry.plugin_name != "crbl-media":
        raise RuntimeError("CRBL Telegram adapter did not replace the native entry")
    registered = getattr(manager, "_plugin_tool_names", None)
    if not isinstance(registered, set) or not expected_tools <= registered:
        raise RuntimeError("CRBL media plugin tool manifest is incomplete")
    resolver = getattr(tools_config, "_get_platform_tools", None)
    if not getattr(resolver, "__crbl_media__", False):
        raise RuntimeError("CRBL role-aware tool resolver is not active")
    if TOOLSETS.get("search", {}).get("tools") != ["web_search"]:
        raise RuntimeError("Hermes search-only toolset contract changed")
    hints = config.get("platform_hints")
    telegram = hints.get("telegram") if isinstance(hints, Mapping) else None
    effective = _resolve_platform_hint(
        SimpleNamespace(_platform_hint_overrides={"telegram": telegram}),
        "telegram",
        PLATFORM_HINTS["telegram"],
    )
    if platform_hint not in effective:
        raise RuntimeError("CRBL media guidance is absent from the effective Telegram prompt")
