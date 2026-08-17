"""All compatibility code for the pinned Hermes v2026.8.3 runtime."""

from __future__ import annotations

import importlib.metadata
import inspect
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

PINNED_HERMES_RELEASE = "v2026.8.3"
PINNED_HERMES_PACKAGE_VERSION = "0.20.0"
NATIVE_MODULE = "plugins.platforms.telegram.adapter"
NATIVE_CLASS = "TelegramAdapter"
NATIVE_CALLBACK_METHOD = "_handle_callback_query"
MEDIA_CALLBACK_PREFIX = "md"

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

    class CompatibleAdapter(candidate):  # type: ignore[valid-type, misc]
        async def _handle_callback_query(self, update: object, context: object) -> None:
            media_handler = getattr(self, "_crbl_media_callback_handler", None)
            if callable(media_handler) and await media_handler(update, self):
                return
            native = getattr(super(), "_handle_callback_query", None)
            if callable(native):
                await native(update, context)

    CompatibleAdapter.__name__ = getattr(candidate, "__name__", "CompatibleTelegramAdapter")
    return CompatibleAdapter


@dataclass(frozen=True)
class MediaCallback:
    """One decoded ``md:`` card tap, with identity taken from Telegram itself."""

    query: Any
    picker_id: str
    action: str
    caller_id: object
    chat_id: object


def read_media_callback(update: object) -> MediaCallback | None:
    """Decode a CRBL media-card callback, or return None to defer to Hermes."""

    query = getattr(update, "callback_query", None)
    if query is None:
        return None
    data = getattr(query, "data", None)
    if not isinstance(data, str) or not data.startswith(f"{MEDIA_CALLBACK_PREFIX}:"):
        return None
    parts = data.split(":", 2)
    picker_id, action = (parts[1], parts[2]) if len(parts) == 3 else ("", "")
    return MediaCallback(
        query=query,
        picker_id=picker_id,
        action=action,
        caller_id=getattr(getattr(query, "from_user", None), "id", None),
        chat_id=getattr(getattr(query, "message", None), "chat_id", None),
    )


async def answer_media_callback(query: Any, text: str | None = None) -> None:
    """Acknowledge a tap. A stale query must never escape the handler."""

    try:
        if text is None:
            await query.answer()
        else:
            await query.answer(text=text)
    except Exception:
        logger.debug("Could not answer Telegram callback query", exc_info=True)


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


def interrupt_running_turn(adapter: object, session_key: str, reason: str) -> bool:
    """Interrupt the exact pinned-Hermes turn that owns an expired picker.

    A gateway clarify timeout normally returns an empty tool result to the
    model, which lets the same research turn continue. Media pickers are a
    terminal handoff to the user instead: once one expires, the agent must stop
    and wait for a new message. Keep this private Hermes traversal in the
    compatibility module so a pin upgrade has one explicit migration seam.
    """

    runner = getattr(adapter, "gateway_runner", None)
    running_agents = getattr(runner, "_running_agents", None)
    get_running = getattr(running_agents, "get", None)
    if not callable(get_running):
        logger.error("Hermes gateway runner does not expose its running-agent map")
        return False
    agent = get_running(session_key)
    interrupt = getattr(agent, "interrupt", None)
    if not callable(interrupt):
        logger.error("Hermes running agent is unavailable for session %s", session_key)
        return False
    try:
        # No interrupt message is intentional. Hermes treats a non-control
        # message as the next queued user turn, which would restart the model
        # loop and recreate the exact unsolicited-search bug this closes.
        interrupt()
    except Exception:
        logger.exception("Could not interrupt expired media-picker turn: %s", reason)
        return False
    logger.info("Interrupted Hermes turn for session %s: %s", session_key, reason)
    return True


def _media_picker_markup(
    *,
    picker_id: str,
    labels: Sequence[str],
    active_index: int,
    active_is_movie: bool,
) -> object:
    from telegram import (  # type: ignore[import-not-found]
        InlineKeyboardButton,
        InlineKeyboardMarkup,
    )

    rows = [
        [
            InlineKeyboardButton(
                f"{'●' if index == active_index else '○'}  {label}",
                callback_data=f"{MEDIA_CALLBACK_PREFIX}:{picker_id}:v{index}",
            )
        ]
        for index, label in enumerate(labels)
    ]
    action = "+ Request movie" if active_is_movie else "✓ Choose series"
    rows.append(
        [
            InlineKeyboardButton(
                action, callback_data=f"{MEDIA_CALLBACK_PREFIX}:{picker_id}:select"
            ),
            InlineKeyboardButton(
                "Cancel", callback_data=f"{MEDIA_CALLBACK_PREFIX}:{picker_id}:cancel"
            ),
        ]
    )
    return InlineKeyboardMarkup(rows)


async def send_media_picker(
    adapter: object,
    *,
    chat_id: int,
    picker_id: str,
    labels: Sequence[str],
    poster_url: str | None,
    caption: str,
    active_index: int,
    active_is_movie: bool,
) -> object:
    """Send one tabbed media card through the pinned Telegram bot."""

    bot = getattr(adapter, "_bot", None)
    if bot is None:
        return SimpleNamespace(success=False)
    markup = _media_picker_markup(
        picker_id=picker_id,
        labels=labels,
        active_index=active_index,
        active_is_movie=active_is_movie,
    )
    if poster_url is not None:
        message = await bot.send_photo(
            chat_id=chat_id,
            photo=poster_url,
            caption=caption,
            parse_mode="HTML",
            reply_markup=markup,
        )
        return SimpleNamespace(success=True, message_id=str(message.message_id), has_photo=True)
    message = await bot.send_message(
        chat_id=chat_id,
        text=caption,
        parse_mode="HTML",
        reply_markup=markup,
        disable_web_page_preview=True,
    )
    return SimpleNamespace(success=True, message_id=str(message.message_id), has_photo=False)


async def edit_media_picker(
    adapter: object,
    query: Any,
    *,
    picker_id: str,
    labels: Sequence[str],
    poster_url: str | None,
    caption: str,
    active_index: int,
    active_is_movie: bool,
    has_photo: bool,
) -> bool:
    """Swap the active tab in place; return whether the message now has a photo."""

    markup = _media_picker_markup(
        picker_id=picker_id,
        labels=labels,
        active_index=active_index,
        active_is_movie=active_is_movie,
    )
    if has_photo and poster_url is not None:
        from telegram import InputMediaPhoto

        await query.edit_message_media(
            media=InputMediaPhoto(media=poster_url, caption=caption, parse_mode="HTML"),
            reply_markup=markup,
        )
        return True
    if not has_photo and poster_url is None:
        await query.edit_message_text(text=caption, parse_mode="HTML", reply_markup=markup)
        return False

    # Telegram cannot convert a text message into a photo or remove a photo
    # in place. Replace only for the uncommon missing-poster transition; the
    # normal poster-to-poster path above remains a true in-place tab switch.
    bot = getattr(adapter, "_bot", None)
    message = getattr(query, "message", None)
    chat_id = getattr(message, "chat_id", None)
    if bot is None or not isinstance(chat_id, int):
        raise RuntimeError("Telegram media card cannot change message kind")
    if poster_url is not None:
        await bot.send_photo(
            chat_id=chat_id,
            photo=poster_url,
            caption=caption,
            parse_mode="HTML",
            reply_markup=markup,
        )
        next_has_photo = True
    else:
        await bot.send_message(
            chat_id=chat_id,
            text=caption,
            parse_mode="HTML",
            reply_markup=markup,
            disable_web_page_preview=True,
        )
        next_has_photo = False
    await query.delete_message()
    return next_has_photo


def _season_label(state: Mapping[str, Any], *, selected: bool) -> str:
    number = state.get("number")
    name = "Specials" if number == 0 else f"S{number}"
    files = state.get("files")
    episodes = state.get("episodes")
    if state.get("complete"):
        # Telegram has no disabled button, so a complete season reads as done
        # and its tap only explains itself.
        return f"☑  {name}  ·  complete"
    counts = f"{files}/{episodes}" if isinstance(episodes, int) and episodes else "not aired yet"
    if state.get("monitored") and not state.get("partial"):
        counts += " · searching"
    box = "☑" if selected else "☐"
    return f"{box}  {name}  ·  {counts}"


def season_picker_markup(
    *,
    picker_id: str,
    states: Sequence[Mapping[str, Any]],
    selected: Sequence[int],
) -> object:
    """Build the season picker: specials last, complete seasons inert."""

    from telegram import (  # type: ignore[import-not-found,unused-ignore]
        InlineKeyboardButton,
        InlineKeyboardMarkup,
    )

    chosen = set(selected)
    rows = [
        [
            InlineKeyboardButton(
                _season_label(state, selected=int(state["number"]) in chosen),
                callback_data=f"{MEDIA_CALLBACK_PREFIX}:{picker_id}:s{state['number']}",
            )
        ]
        for state in states
    ]
    # A season with no episodes yet is still shown, and can be ticked to
    # monitor it, but the shortcut only grabs seasons that actually exist.
    missing = [
        int(state["number"])
        for state in states
        if not state.get("complete") and int(state.get("episodes") or 0) > 0
    ]
    if missing:
        rows.append(
            [
                InlineKeyboardButton(
                    "＋ All missing",
                    callback_data=f"{MEDIA_CALLBACK_PREFIX}:{picker_id}:all",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                f"Request ({len(chosen)})",
                callback_data=f"{MEDIA_CALLBACK_PREFIX}:{picker_id}:go",
            ),
            InlineKeyboardButton(
                "Cancel", callback_data=f"{MEDIA_CALLBACK_PREFIX}:{picker_id}:cancel"
            ),
        ]
    )
    return InlineKeyboardMarkup(rows)


async def send_season_picker(
    adapter: object,
    *,
    chat_id: int,
    picker_id: str,
    poster_url: str | None,
    caption: str,
    states: Sequence[Mapping[str, Any]],
    selected: Sequence[int],
) -> object:
    markup = season_picker_markup(picker_id=picker_id, states=states, selected=selected)
    bot = getattr(adapter, "_bot", None)
    if bot is None:
        return SimpleNamespace(success=False)
    if poster_url is not None:
        message = await bot.send_photo(
            chat_id=chat_id,
            photo=poster_url,
            caption=caption,
            parse_mode="HTML",
            reply_markup=markup,
        )
        return SimpleNamespace(success=True, message_id=str(message.message_id), has_photo=True)
    message = await bot.send_message(
        chat_id=chat_id,
        text=caption,
        parse_mode="HTML",
        reply_markup=markup,
        disable_web_page_preview=True,
    )
    return SimpleNamespace(success=True, message_id=str(message.message_id), has_photo=False)


async def edit_season_selection(
    query: Any,
    *,
    picker_id: str,
    states: Sequence[Mapping[str, Any]],
    selected: Sequence[int],
) -> None:
    """Redraw only the keyboard, so ticking a season does not resend the poster."""

    await query.edit_message_reply_markup(
        reply_markup=season_picker_markup(picker_id=picker_id, states=states, selected=selected)
    )


async def clear_card_buttons(adapter: object, *, chat_id: int, message_id: int) -> None:
    """Drop one card's buttons, leaving its poster and caption in place.

    Used when a request already happened by another route, so the button cannot
    be tapped into a second provider operation.
    """

    bot = getattr(adapter, "_bot", None)
    if bot is None:
        return
    await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)


async def close_media_picker(query: Any, *, caption: str, has_photo: bool) -> None:
    """Remove a card's controls after selection, cancellation, or expiry."""

    if has_photo:
        await query.edit_message_caption(caption=caption, parse_mode="HTML", reply_markup=None)
    else:
        await query.edit_message_text(text=caption, parse_mode="HTML", reply_markup=None)


def _single_result_markup(
    *,
    candidate: Mapping[str, Any],
) -> object | None:
    """Build one action button for a single search result, or None."""

    from telegram import (  # type: ignore[import-not-found,unused-ignore]
        InlineKeyboardButton,
        InlineKeyboardMarkup,
    )

    plex_url = candidate.get("plex_url")
    if isinstance(plex_url, str) and plex_url:
        return InlineKeyboardMarkup(  # type: ignore[no-any-return]
            [[InlineKeyboardButton("▶ Open in Plex", url=plex_url)]]
        )
    media_type = candidate.get("media_type")
    if media_type == "movie":
        # Already available, but with no slug to link to. Offering Request here
        # would invite a redundant request for something the user can already
        # watch, so the card degrades to no button at all. A series is exempt:
        # its button opens the season picker, which stays useful for a complete
        # show because it reports what is held and can re-search a season.
        if candidate.get("downloaded"):
            return None
        tmdb_id = candidate.get("tmdb_id")
        if isinstance(tmdb_id, int) and tmdb_id > 0:
            return InlineKeyboardMarkup(  # type: ignore[no-any-return]
                [
                    [
                        InlineKeyboardButton(
                            "＋ Request",
                            callback_data=f"{MEDIA_CALLBACK_PREFIX}:req:m{tmdb_id}",
                        )
                    ]
                ]
            )
    elif media_type == "series":
        tvdb_id = candidate.get("tvdb_id")
        if isinstance(tvdb_id, int) and tvdb_id > 0:
            return InlineKeyboardMarkup(  # type: ignore[no-any-return]
                [
                    [
                        InlineKeyboardButton(
                            "＋ Request",
                            callback_data=f"{MEDIA_CALLBACK_PREFIX}:req:s{tvdb_id}",
                        )
                    ]
                ]
            )
    return None


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


def verify_pinned_runtime(*, manager: object, expected_tools: set[str], platform_hint: str) -> None:
    """Fail closed when any private pinned-Hermes integration contract moves."""

    from agent import system_prompt  # type: ignore[import-not-found,unused-ignore]
    from agent.prompt_builder import PLATFORM_HINTS  # type: ignore[import-not-found]
    from agent.web_search_registry import (  # type: ignore[import-not-found]
        get_active_extract_provider,
        get_active_search_provider,
    )
    from gateway.platform_registry import platform_registry  # type: ignore[import-not-found]
    from hermes_cli import tools_config
    from toolsets import TOOLSETS  # type: ignore[import-not-found]

    if importlib.metadata.version("hermes-agent") != PINNED_HERMES_PACKAGE_VERSION:
        raise RuntimeError("pinned Hermes package version changed")
    native = native_adapter(SimpleNamespace)
    if native is SimpleNamespace:
        raise RuntimeError("native Hermes Telegram adapter is unavailable")
    if not callable(getattr(native.__base__, NATIVE_CALLBACK_METHOD, None)):
        raise RuntimeError(
            f"native Telegram adapter no longer exposes {NATIVE_CALLBACK_METHOD}; "
            "CRBL media card taps would be silently ignored"
        )
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
    from gateway.platforms.base import BasePlatformAdapter  # type: ignore[import-not-found]
    from gateway.run import GatewayRunner  # type: ignore[import-not-found]
    from run_agent import AIAgent  # type: ignore[import-not-found]

    if not hasattr(BasePlatformAdapter, "gateway_runner"):
        raise RuntimeError("native platform adapter no longer exposes its gateway runner")
    if not isinstance(getattr(GatewayRunner, "_running_agents", None), property):
        raise RuntimeError("Hermes gateway runner no longer exposes its active-turn map")
    if not callable(getattr(AIAgent, "interrupt", None)):
        raise RuntimeError("Hermes agent no longer exposes turn interruption")
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
