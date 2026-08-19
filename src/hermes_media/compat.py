"""All compatibility code for the pinned Hermes v2026.8.3 runtime."""

from __future__ import annotations

import importlib.metadata
import inspect
import logging
from collections.abc import Callable, Mapping
from types import SimpleNamespace
from typing import Any

PINNED_HERMES_RELEASE = "v2026.8.3"
PINNED_HERMES_PACKAGE_VERSION = "0.20.0"
NATIVE_MODULE = "plugins.platforms.telegram.adapter"
NATIVE_CLASS = "TelegramAdapter"

logger = logging.getLogger(__name__)


def native_adapter(fallback: type) -> type:
    """Subclass the native adapter, or return ``fallback`` itself when absent.

    Returning the bare fallback matters: the plugin's registration check is
    ``native_adapter(...) is not _FallbackAdapter``. Wrapping the fallback in a
    subclass would make that identity test pass and register a Telegram bot
    whose ``handle_message`` only echoes, so a missing native adapter has to
    stay detectable by identity.
    """

    try:
        module = __import__(NATIVE_MODULE, fromlist=[NATIVE_CLASS])
        candidate = getattr(module, NATIVE_CLASS)
    except (ImportError, AttributeError):
        return fallback
    if not inspect.isclass(candidate):
        return fallback

    # Nothing to intercept: CRBL cards carry no callback buttons, so Telegram
    # callbacks belong entirely to Hermes.
    return candidate


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


def install_platform_hint(*, platform: str, platform_hint: str) -> None:
    """Append the CRBL guidance to the resolved prompt for one platform.

    Hermes resolves a platform's default hint from its own built-in
    ``PLATFORM_HINTS`` table and only consults a plugin's registered
    ``platform_hint`` for platforms it does not know, so the value CRBL passes
    to ``register_platform`` never reaches a Telegram prompt. The only other
    door is ``config.yaml``'s ``platform_hints`` override, which would make an
    ungoverned file on the host a second source of truth for text that has to
    change in lockstep with the tools it describes.

    Wrapping the resolver keeps that single source in code. The guidance is
    appended to whatever Hermes resolved, so a config override still applies
    and is not silently discarded.
    """

    try:
        from agent import system_prompt  # type: ignore[import-not-found]
    except ImportError:
        return
    original = getattr(system_prompt, "_resolve_platform_hint", None)
    if not callable(original) or getattr(original, "__crbl_media__", False):
        return
    target = platform.lower().strip()

    def resolved(agent: Any, platform_key: str, default_hint: str, *args: Any, **kw: Any) -> str:
        effective = original(agent, platform_key, default_hint, *args, **kw)
        if str(platform_key).lower().strip() != target:
            return str(effective)
        text = str(effective)
        # Idempotent by content as well as by patch flag: a config override
        # that still carries the guidance must not produce it twice.
        if platform_hint in text:
            return text
        return f"{text}\n\n{platform_hint}".strip() if text else platform_hint

    resolved.__crbl_media__ = True  # type: ignore[attr-defined]
    system_prompt._resolve_platform_hint = resolved


def _single_result_markup(*, candidate: Mapping[str, Any]) -> object | None:
    """Offer a link to Plex, or no button at all.

    A card never carries an action. Requesting, choosing between matches and
    picking seasons are all done by talking to the model, so the only thing a
    button can usefully do here is open something the user already has.
    """

    from telegram import (  # type: ignore[import-not-found,unused-ignore]
        InlineKeyboardButton,
        InlineKeyboardMarkup,
    )

    plex_url = candidate.get("plex_url")
    if not isinstance(plex_url, str) or not plex_url:
        return None
    return InlineKeyboardMarkup(  # type: ignore[no-any-return]
        [[InlineKeyboardButton("▶ Open in Plex", url=plex_url)]]
    )


async def send_single_result_card(
    adapter: object,
    *,
    chat_id: int,
    poster_url: str | None,
    caption: str,
    candidate: Mapping[str, Any],
) -> object:
    """Send a single search result with an optional action button."""

    bot = getattr(adapter, "_bot", None)
    if bot is None:
        return SimpleNamespace(success=False)
    markup = _single_result_markup(candidate=candidate)
    if poster_url is not None:
        message = await bot.send_photo(
            chat_id=chat_id,
            photo=poster_url,
            caption=caption,
            parse_mode="HTML",
            reply_markup=markup,
        )
    else:
        message = await bot.send_message(
            chat_id=chat_id,
            text=caption,
            parse_mode="HTML",
            reply_markup=markup,
            disable_web_page_preview=True,
        )
    return SimpleNamespace(success=True, message_id=str(message.message_id))


def verify_pinned_runtime(
    *, manager: object, expected_tools: set[str], platform_hint: str, soul: str
) -> None:
    """Fail closed when any private pinned-Hermes integration contract moves."""

    from agent import system_prompt  # type: ignore[import-not-found,unused-ignore]
    from agent.prompt_builder import (  # type: ignore[import-not-found]
        PLATFORM_HINTS,
        load_soul_md,
    )
    from agent.web_search_registry import (  # type: ignore[import-not-found]
        get_active_extract_provider,
        get_active_search_provider,
    )
    from gateway.platform_registry import platform_registry  # type: ignore[import-not-found]
    from hermes_cli import tools_config
    from toolsets import TOOLSETS  # type: ignore[import-not-found]

    if importlib.metadata.version("hermes-agent") != PINNED_HERMES_PACKAGE_VERSION:
        raise RuntimeError("pinned Hermes package version changed")
    if native_adapter(SimpleNamespace) is SimpleNamespace:
        raise RuntimeError("native Hermes Telegram adapter is unavailable")
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

    # Importing gateway.run can activate Hermes' lazy Telegram platform. Do it
    # only after platform_registry.get() above has loaded CRBL through this
    # manager, or the verifier observes another manager's tool inventory.
    resolver = getattr(tools_config, "_get_platform_tools", None)
    if not getattr(resolver, "__crbl_media__", False):
        raise RuntimeError("CRBL role-aware tool resolver is not active")
    if TOOLSETS.get("search", {}).get("tools") != ["web_search"]:
        raise RuntimeError("Hermes search-only toolset contract changed")
    # Read the resolver off the module, never through a name imported at the
    # top of this function: the plugin -- and therefore the patch -- is only
    # loaded by the platform_registry.get() call above, so an imported name
    # still points at the unpatched original and would fail this check even
    # though the installer ran.
    resolve_hint = getattr(system_prompt, "_resolve_platform_hint", None)
    if not callable(resolve_hint) or not getattr(resolve_hint, "__crbl_media__", False):
        raise RuntimeError("CRBL platform-hint installer is not active")
    if "telegram" not in PLATFORM_HINTS:
        raise RuntimeError("Hermes no longer resolves a built-in Telegram hint")
    # Resolve with no config override at all: the guidance must reach the
    # assembled prompt from the code constant alone, or the single source of
    # truth is not actually installed.
    effective = resolve_hint(
        SimpleNamespace(_platform_hint_overrides={}),
        "telegram",
        PLATFORM_HINTS["telegram"],
    )
    if platform_hint not in effective:
        raise RuntimeError("CRBL media guidance is absent from the effective Telegram prompt")
    # The built-in hint must survive alongside it; replacing Hermes' own
    # Telegram formatting guidance would break MarkdownV2 output.
    if PLATFORM_HINTS["telegram"] not in effective:
        raise RuntimeError("CRBL guidance replaced rather than extended the Telegram hint")
    # The identity slot is an ordinary file read from HERMES_HOME, so a skipped
    # install, a host copy edited by hand and a context limit that truncates the
    # file all present identically: the agent serves without the identity it was
    # built with. Read it back through Hermes' own loader, which is the only
    # reader whose answer matters.
    identity = load_soul_md()
    if identity is None or soul.strip() not in identity:
        raise RuntimeError("CRBL SOUL.md is not the identity Hermes loads")
