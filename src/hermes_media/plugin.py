"""Minimal Hermes v2026.8.3 Telegram adapter and role-aware tools."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import threading
import uuid
from collections.abc import Callable, Coroutine, Mapping
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from media_gateway.constants import ADMIN_UPSTREAM_TOOLS, SHARED_TOOLS
from media_gateway.types import Role

from .client import GatewayClient
from .trusted import (
    actor_from_event,
    actor_scope,
    current_role,
    require_actor,
    require_session_key,
    session_key_from_event,
)

PLATFORM = "telegram"
SHARED_TOOLSET = "crbl-media-shared"
ADMIN_TOOLSET = "crbl-media-admin"
SEARCH_TOOLSET = "search"
WEB_SEARCH_CAP = 10
SEARCH_PRESENTATION_LIMIT = 4
MEDIA_PICKER_TIMEOUT_SECONDS = 120.0
MEDIA_PICKER_SUPERSEDED = "__crbl_media_picker_superseded__"
MEDIA_PICKER_EXPIRED = "__crbl_media_picker_expired__"
PLATFORM_HINT = (
    "Telegram identity is trusted automatically; never request user IDs. "
    "For every movie or series title lookup, availability check, or request, call "
    "search_media in the current turn before answering or changing anything. Never "
    "reuse search results from conversation history. search_media itself handles any "
    "ambiguous-result selection before it returns. Never repeat its candidate list or "
    "call clarify for those same candidates; answer only about its returned result."
)
NATIVE_MODULE = "plugins.platforms.telegram.adapter"
NATIVE_CLASS = "TelegramAdapter"
logger = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class _PendingMediaPicker:
    clarify_id: str
    choices: tuple[str, ...]


_pending_picker_lock = threading.Lock()
_pending_pickers: dict[str, _PendingMediaPicker] = {}


def _gateway() -> GatewayClient:
    global _client
    if _client is None:
        _client = GatewayClient.from_env()
    return _client


class MediaTelegramAdapter(_NativeAdapter):  # type: ignore[misc, valid-type]
    def __init__(self, config: object) -> None:
        super().__init__(config)
        self._media_delivery_loop: asyncio.AbstractEventLoop | None = None

    @property
    def authorization_is_upstream(self) -> bool:
        """Report the gateway's authenticated actor decision to Hermes."""

        return True

    def _is_user_authorized_from_message(self, message: object) -> bool:
        """Defer the intake decision to the gateway's live file-backed policy.

        Hermes' native Telegram prefilter reads the process environment, which
        does not change when the dashboard atomically edits the policy file.
        Passing intake here is safe because ``handle_message`` extracts the
        native Telegram actor and rejects blocked users before calling the
        native handler or the model.
        """

        del message
        return True

    async def handle_message(self, event: object) -> object:
        self._media_delivery_loop = asyncio.get_running_loop()
        actor = actor_from_event(event)
        extra = getattr(self.config, "extra", None)
        extra = extra if isinstance(extra, Mapping) else {}
        session_key = session_key_from_event(
            event,
            actor,
            group_sessions_per_user=bool(extra.get("group_sessions_per_user", True)),
            thread_sessions_per_user=bool(extra.get("thread_sessions_per_user", False)),
        )
        role = await _gateway().observe(actor)
        if role is Role.BLOCKED:
            raise PermissionError("Telegram user is not allowed")
        with actor_scope(actor, role, session_key):
            text = _event_text(event)
            if text and not text.startswith("/"):
                picker_action = await _resolve_pending_picker_text(session_key, text)
                if picker_action == "selected":
                    return None
            result = super().handle_message(event)
            return await result if inspect.isawaitable(result) else result


_active_adapter: MediaTelegramAdapter | None = None


def _event_text(event: object) -> str:
    value = getattr(event, "text", None)
    if not isinstance(value, str):
        raw_message = getattr(event, "raw_message", None)
        value = getattr(raw_message, "text", None)
    return value.strip() if isinstance(value, str) else ""


def _set_pending_picker(session_key: str, pending: _PendingMediaPicker) -> None:
    with _pending_picker_lock:
        _pending_pickers[session_key] = pending


def _get_pending_picker(session_key: str) -> _PendingMediaPicker | None:
    with _pending_picker_lock:
        return _pending_pickers.get(session_key)


def _discard_pending_picker(session_key: str, clarify_id: str) -> None:
    with _pending_picker_lock:
        current = _pending_pickers.get(session_key)
        if current is not None and current.clarify_id == clarify_id:
            _pending_pickers.pop(session_key, None)
    adapter = _active_adapter
    native_state = getattr(adapter, "_clarify_state", None)
    if isinstance(native_state, dict):
        native_state.pop(clarify_id, None)


def _picker_choice(text: str, choices: tuple[str, ...]) -> str | None:
    """Resolve only unambiguous selection-shaped text.

    Everything else is a new conversational request and supersedes the
    picker. This keeps a title such as ``avengers`` from becoming free-form
    input to the previous media search.
    """

    normalized = " ".join(text.casefold().split())
    if not normalized:
        return None
    if normalized.isdecimal():
        index = int(normalized)
        if 1 <= index <= len(choices):
            return choices[index - 1]
    exact = [choice for choice in choices if " ".join(choice.casefold().split()) == normalized]
    if len(exact) == 1:
        return exact[0]
    if re.fullmatch(r"\d{4}", normalized):
        by_year = [choice for choice in choices if f"({normalized})" in choice]
        if len(by_year) == 1:
            return by_year[0]
    by_title = []
    for choice in choices:
        title = choice.split(" · ", 1)[0]
        title = re.sub(r"\s+\(\d{4}\)$", "", title)
        if " ".join(title.casefold().split()) == normalized:
            by_title.append(choice)
    return by_title[0] if len(by_title) == 1 else None


def _picker_timeout(configured_timeout: float) -> float:
    if configured_timeout <= 0:
        return MEDIA_PICKER_TIMEOUT_SECONDS
    return min(configured_timeout, MEDIA_PICKER_TIMEOUT_SECONDS)


async def _resolve_pending_picker_text(session_key: str, text: str) -> str:
    """Select an active picker or cancel it in favor of a fresh message."""

    pending = _get_pending_picker(session_key)
    if pending is None:
        return "none"
    selection = _picker_choice(text, pending.choices)
    response = selection or MEDIA_PICKER_SUPERSEDED
    clarify = _clarify_gateway()
    if not clarify.resolve_gateway_clarify(pending.clarify_id, response):
        _discard_pending_picker(session_key, pending.clarify_id)
        return "none"

    # Drain the native entry immediately. The original tool waiter owns the
    # same event object and receives the same response, while the new message
    # can no longer be mistaken for a second answer to the stale picker.
    await asyncio.to_thread(clarify.wait_for_response, pending.clarify_id, 0.05)
    _discard_pending_picker(session_key, pending.clarify_id)
    return "selected" if selection is not None else "superseded"


def _candidate_label(candidate: object) -> str | None:
    if not isinstance(candidate, Mapping):
        return None
    title = candidate.get("title")
    media_type = candidate.get("media_type")
    if not isinstance(title, str) or not title.strip() or media_type not in {"movie", "series"}:
        return None
    clean_title = " ".join(title.split())[:160]
    year = candidate.get("year")
    year_text = f" ({year})" if isinstance(year, int) and not isinstance(year, bool) else ""
    if media_type == "movie":
        external_id = candidate.get("tmdb_id")
        if isinstance(external_id, bool) or not isinstance(external_id, int) or external_id <= 0:
            return None
        return f"{clean_title}{year_text} · Movie · TMDB {external_id}"
    external_id = candidate.get("tvdb_id")
    if isinstance(external_id, bool) or not isinstance(external_id, int) or external_id <= 0:
        return None
    return f"{clean_title}{year_text} · Series · TVDB {external_id}"


def _safe_poster_url(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 2048:
        return None
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    try:
        from tools.url_safety import is_safe_url  # type: ignore[import-not-found]
    except ImportError:
        return value
    try:
        return value if is_safe_url(value) else None
    except (OSError, ValueError):
        return None


def _search_presentation(
    result: Mapping[str, Any],
) -> tuple[list[tuple[str, str]], list[str], list[dict[str, Any]]]:
    rows = result.get("results")
    if not isinstance(rows, list):
        return [], [], []
    cards: list[tuple[str, str]] = []
    choices: list[str] = []
    candidates: list[dict[str, Any]] = []
    for candidate in rows:
        if len(choices) == SEARCH_PRESENTATION_LIMIT:
            break
        label = _candidate_label(candidate)
        if label is None:
            continue
        index = len(choices) + 1
        choices.append(label)
        candidates.append(dict(candidate))
        if isinstance(candidate, Mapping):
            poster_url = _safe_poster_url(candidate.get("poster_url"))
            if poster_url is not None:
                cards.append((poster_url, f"{index} · {label}"))
    return cards, choices, candidates


async def _on_adapter_loop(
    call: Callable[[], Coroutine[Any, Any, Any]],
    *,
    timeout: float = 30,
) -> Any:
    adapter = _active_adapter
    if adapter is None:
        return None
    target_loop = adapter._media_delivery_loop
    if target_loop is None or target_loop.is_closed():
        return None
    if asyncio.get_running_loop() is target_loop:
        return await call()
    future: Future[Any] = asyncio.run_coroutine_threadsafe(call(), target_loop)
    try:
        return await asyncio.wait_for(asyncio.wrap_future(future), timeout=timeout)
    except TimeoutError:
        future.cancel()
        raise


async def _deliver_search_cards(actor_chat_id: int, cards: list[tuple[str, str]]) -> bool:
    adapter = _active_adapter
    if adapter is None or not cards:
        return False

    async def deliver() -> bool:
        if len(cards) == 1:
            send_image = getattr(adapter, "send_image", None)
            if not callable(send_image):
                return False
            response = await send_image(
                chat_id=str(actor_chat_id),
                image_url=cards[0][0],
                caption=cards[0][1],
            )
            return bool(getattr(response, "success", False))
        send_multiple = getattr(adapter, "send_multiple_images", None)
        if not callable(send_multiple):
            return False
        await send_multiple(chat_id=str(actor_chat_id), images=cards)
        return True

    try:
        return bool(await _on_adapter_loop(deliver))
    except TimeoutError:
        logger.warning("Telegram poster album delivery timed out")
        return False


def _clarify_gateway() -> Any:
    from tools import clarify_gateway  # type: ignore[import-not-found]

    return clarify_gateway


async def _select_search_result(
    actor_chat_id: int,
    session_key: str,
    choices: list[str],
) -> str | None:
    adapter = _active_adapter
    send_clarify = getattr(adapter, "send_clarify", None)
    if adapter is None or not callable(send_clarify) or len(choices) < 2:
        return choices[0] if len(choices) == 1 else None

    clarify = _clarify_gateway()
    clarify_id = uuid.uuid4().hex[:10]
    clarify.register(
        clarify_id=clarify_id,
        session_key=session_key,
        question="Which result did you mean?",
        choices=choices,
        multi_select=False,
    )
    pending = _PendingMediaPicker(clarify_id=clarify_id, choices=tuple(choices))
    previous = _get_pending_picker(session_key)
    if previous is not None:
        clarify.resolve_gateway_clarify(previous.clarify_id, MEDIA_PICKER_SUPERSEDED)
        await asyncio.to_thread(clarify.wait_for_response, previous.clarify_id, 0.05)
        _discard_pending_picker(session_key, previous.clarify_id)
    _set_pending_picker(session_key, pending)

    async def deliver() -> bool:
        response = await send_clarify(
            chat_id=str(actor_chat_id),
            question="Which result did you mean?",
            choices=choices,
            clarify_id=clarify_id,
            session_key=session_key,
            metadata=None,
        )
        return bool(getattr(response, "success", False))

    try:
        try:
            delivered = bool(await _on_adapter_loop(deliver, timeout=15))
        except Exception:
            logger.warning("Telegram media picker delivery failed", exc_info=True)
            delivered = False
        if not delivered:
            clarify.resolve_gateway_clarify(clarify_id, "")
            await asyncio.to_thread(clarify.wait_for_response, clarify_id, 0.01)
            return None

        timeout = _picker_timeout(float(clarify.get_clarify_timeout()))
        response = await asyncio.to_thread(clarify.wait_for_response, clarify_id, timeout)
        if isinstance(response, str) and response.strip():
            return response.strip()
        return MEDIA_PICKER_EXPIRED
    finally:
        _discard_pending_picker(session_key, clarify_id)


async def _decorate_search_result(
    actor_chat_id: int,
    session_key: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    cards, choices, candidates = _search_presentation(result)
    try:
        delivered = await _deliver_search_cards(actor_chat_id, cards)
    except Exception:
        logger.warning("Telegram poster album delivery failed", exc_info=True)
        delivered = False
    if not candidates:
        return result
    decorated = dict(result)
    decorated["results"] = candidates
    if len(candidates) == 1:
        decorated["telegram_presentation"] = {
            "poster_cards_delivered": delivered,
            "poster_count": len(cards),
            "selection_status": "single_result",
            "instruction": "Answer only about the single returned result.",
        }
        return decorated

    selection = await _select_search_result(actor_chat_id, session_key, choices)
    if selection == MEDIA_PICKER_SUPERSEDED:
        decorated["results"] = []
        decorated["telegram_presentation"] = {
            "poster_cards_delivered": delivered,
            "poster_count": len(cards),
            "selection_status": "superseded",
            "instruction": (
                "The user sent a new request instead of selecting this result. End this turn "
                "without a user-facing response; the new request is queued separately."
            ),
        }
        return decorated
    if selection == MEDIA_PICKER_EXPIRED:
        decorated["results"] = []
        decorated["telegram_presentation"] = {
            "poster_cards_delivered": delivered,
            "poster_count": len(cards),
            "selection_status": "expired",
            "instruction": (
                "The unanswered result picker expired. End this turn without a user-facing "
                "response; a later request must perform a fresh search."
            ),
        }
        return decorated
    if selection in choices:
        selected_index = choices.index(selection)
        decorated["results"] = [candidates[selected_index]]
        decorated["telegram_presentation"] = {
            "poster_cards_delivered": delivered,
            "poster_count": len(cards),
            "selection_status": "selected",
            "selected_choice": selection,
            "instruction": (
                "The user selected this exact result in Telegram. Answer only about the "
                "single returned result; do not repeat the candidate list or search again."
            ),
        }
        return decorated

    decorated["results"] = []
    decorated["telegram_presentation"] = {
        "poster_cards_delivered": delivered,
        "poster_count": len(cards),
        "selection_status": "unavailable",
        "instruction": (
            "The result picker could not obtain a selection. Do not request anything, do not "
            "guess a candidate, and do not repeat the stale candidate list."
        ),
    }
    return decorated


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
        # Hermes' native ``search`` toolset contains only ``web_search``.
        # Do not expose the broader ``web`` toolset: it also includes
        # arbitrary page extraction, which is outside this bot's boundary.
        toolsets = {SHARED_TOOLSET, SEARCH_TOOLSET}
        if current_role() is Role.ADMIN:
            toolsets.add(ADMIN_TOOLSET)
        return toolsets

    visible.__crbl_media__ = True  # type: ignore[attr-defined]
    tools_config._get_platform_tools = visible


def validate_search_guardrail(config: Mapping[str, Any]) -> None:
    """Require the native agent and startup verifier to share one search cap."""

    guardrails = config.get("tool_loop_guardrails")
    loop_caps = guardrails.get("loop_caps") if isinstance(guardrails, Mapping) else None
    cap = loop_caps.get("max_web_searches") if isinstance(loop_caps, Mapping) else None
    if cap != WEB_SEARCH_CAP:
        raise RuntimeError(
            "Hermes config must set "
            f"tool_loop_guardrails.loop_caps.max_web_searches to {WEB_SEARCH_CAP}"
        )


def validate_platform_hint(config: Mapping[str, Any]) -> None:
    """Require the CRBL guidance in Hermes' effective Telegram override."""

    hints = config.get("platform_hints")
    telegram = hints.get(PLATFORM) if isinstance(hints, Mapping) else None
    append = telegram.get("append") if isinstance(telegram, Mapping) else None
    if append != PLATFORM_HINT:
        raise RuntimeError(
            "Hermes config must append the CRBL media guidance at platform_hints.telegram.append"
        )


def _handler(name: str) -> Callable[..., Coroutine[Any, Any, str]]:
    async def call(arguments: Mapping[str, Any], **runtime: Any) -> str:
        del runtime
        actor = require_actor()
        result = await _gateway().call(actor, name, dict(arguments))
        if name == "search_media" and isinstance(result.get("results"), list):
            result = await _decorate_search_result(
                actor.chat_id,
                require_session_key(),
                result,
            )
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))

    return call


def _adapter(config: object) -> MediaTelegramAdapter:
    global _active_adapter
    _active_adapter = MediaTelegramAdapter(config)
    return _active_adapter


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
        platform_hint=PLATFORM_HINT,
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
