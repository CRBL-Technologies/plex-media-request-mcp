"""Minimal Hermes v2026.8.3 Telegram adapter and role-aware tools."""

from __future__ import annotations

import asyncio
import html
import inspect
import json
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Callable, Coroutine, Mapping
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from media_gateway.constants import ADMIN_UPSTREAM_TOOLS, SHARED_TOOLS
from media_gateway.types import Actor, Role

from .client import GatewayClient, GatewayError
from .compat import (
    answer_media_callback,
    close_media_picker,
    edit_media_picker,
    install_tool_visibility,
    interrupt_running_turn,
    native_adapter,
    read_media_callback,
    send_media_picker,
    send_single_result_card,
)
from .trusted import (
    actor_from_event,
    actor_scope,
    current_role,
    current_turn_text,
    is_recommendation_turn,
    require_actor,
    require_session_key,
    session_key_from_event,
)

PLATFORM = "telegram"
SHARED_TOOLSET = "crbl-media-shared"
ADMIN_TOOLSET = "crbl-media-admin"
SEARCH_TOOLSET = "search"
WEB_SEARCH_CAP = 10
RECOMMENDATION_CONTEXT_SECONDS = 30 * 60
SEARCH_PRESENTATION_LIMIT = 4
BUTTON_LABEL_LIMIT = 34
CAPTION_HEADING_LIMIT = 200
CAPTION_OVERVIEW_LIMIT = 420
MEDIA_PICKER_TIMEOUT_SECONDS = 120.0
MEDIA_PICKER_SUPERSEDED = "__crbl_media_picker_superseded__"
MEDIA_PICKER_EXPIRED = "__crbl_media_picker_expired__"
MEDIA_PICKER_CANCELLED = "__crbl_media_picker_cancelled__"
MEDIA_PICKER_REQUESTED = "__crbl_media_picker_requested__:"
MEDIA_PICKER_REQUEST_FAILED = "__crbl_media_picker_request_failed__:"
PLATFORM_HINT = (
    "Telegram identity is trusted automatically; never request user IDs. "
    "For every direct movie or series title lookup, availability check, or request, call "
    "search_media in the current turn before answering or changing anything. Never "
    "reuse search results from conversation history. search_media itself handles any "
    "ambiguous-result selection before it returns. Never repeat its candidate list or "
    "call clarify for those same candidates. For a recommendation request, use web_search to "
    "choose exactly 4 distinct titles, then call recommend_media exactly once with those exact "
    "titles and years; never call search_media separately for each recommendation. "
    "recommend_media returns results for a conversational reply; present each title with its "
    "availability and offer to add any that are missing. Discovery "
    "turns reject model-generated single-title searches before a card is sent. If the user's "
    "current message explicitly says "
    "to add or request media, continue to request the selected result, whether it was chosen by "
    "button or by typed reply. The Telegram media card's Request movie action performs the "
    "request itself before the tool returns; when the result reports the request already "
    "happened, confirm the outcome and never call request_movie for it again. Choosing a series "
    "identifies it but still requires the desired seasons. A title-only lookup remains read-only "
    "until the user presses an action or asks for the request in the same message."
)
logger = logging.getLogger(__name__)


class _FallbackAdapter:
    def __init__(self, config: object) -> None:
        self.config = config

    async def handle_message(self, event: object) -> object:
        return event


_NativeAdapter = native_adapter(_FallbackAdapter)
_client: GatewayClient | None = None


@dataclass
class _PendingMediaPicker:
    clarify_id: str
    choices: tuple[str, ...]
    session_key: str = ""
    candidates: tuple[dict[str, Any], ...] = ()
    posters: tuple[str | None, ...] = ()
    actor_user_id: int = 0
    actor_chat_id: int = 0
    active_index: int = 0
    has_photo: bool = False


_pending_picker_lock = threading.Lock()
_pending_pickers: dict[str, _PendingMediaPicker] = {}
_recommendation_context_lock = threading.Lock()
_recommendation_contexts: dict[str, float] = {}


_RECOMMENDATION_PATTERNS = (
    re.compile(r"\b(?:recommend|recommendation|suggest|suggestion)s?\b", re.IGNORECASE),
    re.compile(r"\bsomething\b", re.IGNORECASE),
    re.compile(r"\b(?:what|which)\b.{0,40}\bwatch\b", re.IGNORECASE),
    re.compile(r"\b(?:movie|show|series|film)s?\b.{0,30}\bfor tonight\b", re.IGNORECASE),
    re.compile(r"\b(?:watch|watching)\b.{0,20}\btonight\b", re.IGNORECASE),
    re.compile(r"\bsimilar to\b", re.IGNORECASE),
)


def _gateway() -> GatewayClient:
    global _client
    if _client is None:
        _client = GatewayClient.from_env()
    return _client


def _looks_like_recommendation(text: str) -> bool:
    return any(pattern.search(text) for pattern in _RECOMMENDATION_PATTERNS)


def _recommendation_context(session_key: str, text: str) -> bool:
    now = time.monotonic()
    with _recommendation_context_lock:
        expired = [key for key, deadline in _recommendation_contexts.items() if deadline <= now]
        for key in expired:
            _recommendation_contexts.pop(key, None)
        if _looks_like_recommendation(text):
            _recommendation_contexts[session_key] = now + RECOMMENDATION_CONTEXT_SECONDS
        return session_key in _recommendation_contexts


def _clear_recommendation_context(session_key: str) -> None:
    with _recommendation_context_lock:
        _recommendation_contexts.pop(session_key, None)


def _normalized_words(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", value.casefold())


def _query_is_from_current_message(query: object, message: str) -> bool:
    if not isinstance(query, str):
        return False
    query_words = _normalized_words(query)
    message_words = _normalized_words(message)
    if not query_words or not message_words:
        return False
    width = len(query_words)
    return any(
        message_words[index : index + width] == query_words for index in range(len(message_words))
    )


class MediaTelegramAdapter(_NativeAdapter):  # type: ignore[misc, valid-type]
    def __init__(self, config: object) -> None:
        super().__init__(config)
        self._media_delivery_loop: asyncio.AbstractEventLoop | None = None
        self._crbl_media_callback_handler = _handle_media_picker_callback

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
        text = _event_text(event)
        recommendation_turn = _recommendation_context(session_key, text)
        with actor_scope(
            actor,
            role,
            session_key,
            turn_text=text,
            recommendation_turn=recommendation_turn,
        ):
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
        pending.session_key = session_key
        _pending_pickers[session_key] = pending


def _get_pending_picker(session_key: str) -> _PendingMediaPicker | None:
    with _pending_picker_lock:
        return _pending_pickers.get(session_key)


def _pending_picker_by_id(clarify_id: str) -> _PendingMediaPicker | None:
    with _pending_picker_lock:
        return next(
            (pending for pending in _pending_pickers.values() if pending.clarify_id == clarify_id),
            None,
        )


def _discard_pending_picker(session_key: str, clarify_id: str) -> None:
    with _pending_picker_lock:
        current = _pending_pickers.get(session_key)
        if current is not None and current.clarify_id == clarify_id:
            _pending_pickers.pop(session_key, None)


def _set_picker_photo(pending: _PendingMediaPicker, has_photo: bool) -> None:
    with _pending_picker_lock:
        pending.has_photo = has_photo


def _commit_picker_tab(pending: _PendingMediaPicker, index: int, has_photo: bool) -> None:
    """Advance the active tab only once its card has actually been redrawn."""

    with _pending_picker_lock:
        pending.active_index = index
        pending.has_photo = has_photo


def _read_picker_tab(pending: _PendingMediaPicker) -> tuple[int, bool]:
    with _pending_picker_lock:
        return pending.active_index, pending.has_photo


def _picker_poster(pending: _PendingMediaPicker, index: int) -> str | None:
    return pending.posters[index] if index < len(pending.posters) else None


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
    if selection is None:
        interrupt_running_turn(
            _active_adapter,
            session_key,
            "Telegram media picker superseded by a new message",
        )
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


def _candidate_view(candidate: Mapping[str, Any]) -> tuple[str, str, bool]:
    """Normalize one candidate into (title, year text, is_movie) exactly once."""

    title = " ".join(str(candidate.get("title") or "Unknown title").split())
    year = candidate.get("year")
    year_text = str(year) if isinstance(year, int) and not isinstance(year, bool) else ""
    return title, year_text, candidate.get("media_type") == "movie"


def _clip(value: str, maximum: int) -> str:
    if len(value) <= maximum:
        return value
    return value[: max(1, maximum - 1)].rstrip() + "…"


def _candidate_button_label(candidate: Mapping[str, Any]) -> str:
    title, year_text, is_movie = _candidate_view(candidate)
    suffix = f" · {year_text}" if year_text else ""
    icon = "🎬" if is_movie else "📺"
    return f"{icon} {_clip(title, max(8, BUTTON_LABEL_LIMIT - len(suffix)))}{suffix}"


def _candidate_caption(candidate: Mapping[str, Any]) -> str:
    title, year_text, is_movie = _candidate_view(candidate)
    suffix = f" ({year_text})" if year_text else ""
    kind = "Movie" if is_movie else "Series"
    overview = " ".join(str(candidate.get("overview") or "").split())
    # Escape first, then clip. Clipping raw text lets entity expansion
    # ("&" -> "&amp;") push the caption past Telegram's 1024-character limit.
    heading = (
        f"<b>{_clip(html.escape(_clip(title, 160)), CAPTION_HEADING_LIMIT)}{suffix}</b> · {kind}"
    )
    detail = _clip(html.escape(overview), CAPTION_OVERVIEW_LIMIT)
    return f"{heading}\n\n{detail}" if detail else heading


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
) -> tuple[list[tuple[str, str]], list[str], list[dict[str, Any]], list[str | None]]:
    """Validate every poster once, off the Telegram event loop.

    ``is_safe_url`` performs a blocking DNS resolve, so it must not run on the
    adapter or callback loop. ``posters`` stays index-aligned with
    ``candidates``; ``cards`` keeps only the ones that have artwork.
    """

    rows = result.get("results")
    if not isinstance(rows, list):
        return [], [], [], []
    cards: list[tuple[str, str]] = []
    choices: list[str] = []
    candidates: list[dict[str, Any]] = []
    posters: list[str | None] = []
    for candidate in rows:
        if len(choices) == SEARCH_PRESENTATION_LIMIT:
            break
        label = _candidate_label(candidate)
        if label is None:
            continue
        index = len(choices) + 1
        choices.append(label)
        candidates.append(dict(candidate))
        poster_url = (
            _safe_poster_url(candidate.get("poster_url"))
            if isinstance(candidate, Mapping)
            else None
        )
        posters.append(poster_url)
        if poster_url is not None:
            cards.append((poster_url, f"{index} · {label}"))
    return cards, choices, candidates, posters


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


def _clarify_gateway() -> Any:
    from tools import clarify_gateway  # type: ignore[import-not-found]

    return clarify_gateway


async def _select_search_result(
    actor_chat_id: int,
    actor_user_id: int,
    session_key: str,
    choices: list[str],
    candidates: list[dict[str, Any]],
    posters: list[str | None],
) -> str | None:
    adapter = _active_adapter
    if adapter is None or len(choices) < 2:
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
    pending = _PendingMediaPicker(
        clarify_id=clarify_id,
        choices=tuple(choices),
        session_key=session_key,
        candidates=tuple(candidates),
        posters=tuple(posters),
        actor_user_id=actor_user_id,
        actor_chat_id=actor_chat_id,
    )
    previous = _get_pending_picker(session_key)
    if previous is not None:
        clarify.resolve_gateway_clarify(previous.clarify_id, MEDIA_PICKER_SUPERSEDED)
        await asyncio.to_thread(clarify.wait_for_response, previous.clarify_id, 0.05)
        _discard_pending_picker(session_key, previous.clarify_id)
    _set_pending_picker(session_key, pending)

    async def deliver() -> bool:
        candidate = candidates[0]
        response = await send_media_picker(
            adapter,
            chat_id=actor_chat_id,
            picker_id=clarify_id,
            labels=[_candidate_button_label(item) for item in candidates],
            poster_url=posters[0] if posters else None,
            caption=_candidate_caption(candidate),
            active_index=0,
            active_is_movie=candidate.get("media_type") == "movie",
        )
        _set_picker_photo(pending, bool(getattr(response, "has_photo", False)))
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
        interrupt_running_turn(adapter, session_key, "Telegram media picker expired")
        return MEDIA_PICKER_EXPIRED
    finally:
        _discard_pending_picker(session_key, clarify_id)


async def _handle_single_result_callback(query: Any, action: str) -> bool:
    """Handle a tap on a single-result card's action button."""

    if action.startswith("m"):
        # Movie request: md:req:m<tmdb_id>
        try:
            tmdb_id = int(action[1:])
        except ValueError:
            await answer_media_callback(query, "Invalid request.")
            return True
        await answer_media_callback(query, "Requesting…")
        caller_id = getattr(getattr(query, "from_user", None), "id", None)
        chat_id = getattr(getattr(query, "message", None), "chat_id", None)
        if not isinstance(caller_id, int) or not isinstance(chat_id, int):
            return True
        actor = Actor(user_id=caller_id, chat_id=chat_id)
        try:
            outcome = await _gateway().call(actor, "request_movie", {"tmdb_id": tmdb_id})
            request_status = str(outcome.get("status") or "requested")
            note = {
                "available": "Already on Plex ✓",
                "awaiting_plex": "Already added — waiting for Plex to import it.",
            }.get(request_status, "Requested ✓")
        except (GatewayError, OSError):
            logger.warning("Single-result request_movie failed", exc_info=True)
            note = "Request failed — ask me to try again."
        try:
            # python-telegram-bot returns an empty tuple, not None, for a
            # message without a photo, so this must test truthiness. An
            # is-not-None check leaves every text card's button live and lets
            # a second tap fire a duplicate request.
            has_photo = bool(getattr(getattr(query, "message", None), "photo", None))
            await close_media_picker(query, caption=f"<i>{note}</i>", has_photo=has_photo)
        except Exception:
            logger.debug("Could not update single-result card", exc_info=True)
        return True

    if action.startswith("s"):
        # Series request: md:req:s<tvdb_id>
        await answer_media_callback(query, "Which seasons do you want?")
        return True

    await answer_media_callback(query, "Invalid request.")
    return True


async def _handle_media_picker_callback(update: object, adapter: object) -> bool:
    """Handle one tab switch or action without involving the model."""

    callback = read_media_callback(update)
    if callback is None:
        return False
    query = callback.query

    # Single-result action buttons use the "req" picker_id prefix.
    if callback.picker_id == "req" and callback.action:
        return await _handle_single_result_callback(query, callback.action)

    if not callback.picker_id or not callback.action:
        await answer_media_callback(query, "Invalid media card.")
        return True
    pending = _pending_picker_by_id(callback.picker_id)
    if pending is None:
        await answer_media_callback(query, "This media card has expired.")
        return True
    if callback.caller_id != pending.actor_user_id or callback.chat_id != pending.actor_chat_id:
        await answer_media_callback(query, "⛔ This media card belongs to another request.")
        return True

    if callback.action.startswith("v"):
        try:
            index = int(callback.action[1:])
        except ValueError:
            await answer_media_callback(query, "Invalid result.")
            return True
        if not 0 <= index < len(pending.candidates):
            await answer_media_callback(query, "Invalid result.")
            return True
        _, has_photo = _read_picker_tab(pending)
        try:
            next_has_photo = await edit_media_picker(
                adapter,
                query,
                picker_id=callback.picker_id,
                labels=[_candidate_button_label(item) for item in pending.candidates],
                poster_url=_picker_poster(pending, index),
                caption=_candidate_caption(pending.candidates[index]),
                active_index=index,
                active_is_movie=pending.candidates[index].get("media_type") == "movie",
                has_photo=has_photo,
            )
        except Exception:
            logger.warning("Telegram media tab switch failed", exc_info=True)
            await answer_media_callback(query, "Could not open that result. Try again.")
            return True
        _commit_picker_tab(pending, index, next_has_photo)
        await answer_media_callback(query)
        return True

    if callback.action not in {"select", "cancel"}:
        await answer_media_callback(query, "Invalid media action.")
        return True
    active_index, has_photo = _read_picker_tab(pending)
    candidate = pending.candidates[active_index]
    choice = pending.choices[active_index]
    is_movie = candidate.get("media_type") == "movie"

    if callback.action == "select" and is_movie:
        await answer_media_callback(query, "Requesting…")
        actor = Actor(user_id=pending.actor_user_id, chat_id=pending.actor_chat_id)
        tmdb_id = candidate.get("tmdb_id")
        try:
            outcome = await _gateway().call(actor, "request_movie", {"tmdb_id": tmdb_id})
            request_status = str(outcome.get("status") or "requested")
            response = f"{MEDIA_PICKER_REQUESTED}{request_status}:{choice}"
            note = {
                "available": "Already on Plex ✓",
                "awaiting_plex": "Already added — waiting for Plex to import it.",
            }.get(request_status, "Requested ✓ — you'll get a message when it's on Plex.")
        except (GatewayError, OSError):
            logger.warning("Media card request_movie failed", exc_info=True)
            response = f"{MEDIA_PICKER_REQUEST_FAILED}{choice}"
            note = "Request failed — ask me to try again."
        clarify = _clarify_gateway()
        clarify.resolve_gateway_clarify(callback.picker_id, response)
        try:
            status = f"{_candidate_caption(candidate)}\n\n<i>{note}</i>"
            await close_media_picker(query, caption=status, has_photo=has_photo)
        except Exception:
            logger.debug("Could not close resolved media card", exc_info=True)
        return True

    response = {
        "select": choice,
        "cancel": MEDIA_PICKER_CANCELLED,
    }[callback.action]
    clarify = _clarify_gateway()
    if not clarify.resolve_gateway_clarify(callback.picker_id, response):
        await answer_media_callback(query, "This media card has expired.")
        return True
    if response == MEDIA_PICKER_CANCELLED:
        interrupt_running_turn(
            adapter or _active_adapter,
            pending.session_key,
            "Telegram media picker cancelled",
        )
    acknowledgement = "Cancelled" if response == MEDIA_PICKER_CANCELLED else "Selected"
    await answer_media_callback(query, acknowledgement)
    try:
        if response == MEDIA_PICKER_CANCELLED:
            status = "<i>Media selection cancelled.</i>"
        else:
            status = f"{_candidate_caption(candidate)}\n\n<i>Selected.</i>"
        await close_media_picker(query, caption=status, has_photo=has_photo)
    except Exception:
        logger.debug("Could not close resolved media card", exc_info=True)
    return True


async def _decorate_search_result(
    actor_chat_id: int,
    session_key: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    recommendation_mode = result.get("presentation") == "recommendations"
    cards, choices, candidates, posters = _search_presentation(result)
    if not candidates:
        return result
    decorated = dict(result)
    decorated["results"] = candidates

    # --- Recommendations: conversational reply, no picker ---
    if recommendation_mode:
        _clear_recommendation_context(session_key)
        decorated["telegram_presentation"] = {
            "poster_cards_delivered": False,
            "poster_count": 0,
            "selection_status": "conversational",
            "instruction": (
                "Present these recommendations conversationally. For each title, state its "
                "name, year, media type, and whether it is on Plex. Offer to add any that are "
                "missing. Do not open a picker or repeat raw candidate data."
            ),
        }
        return decorated

    # --- Single result: poster card with action button ---
    if len(candidates) == 1:
        delivered = False
        adapter = _active_adapter
        candidate = candidates[0]
        poster = posters[0] if posters else None
        if adapter is not None:

            async def deliver_single() -> bool:
                response = await send_single_result_card(
                    adapter,
                    chat_id=actor_chat_id,
                    poster_url=poster,
                    caption=_candidate_caption(candidate),
                    candidate=candidate,
                )
                return bool(getattr(response, "success", False))

            try:
                delivered = bool(await _on_adapter_loop(deliver_single))
            except Exception:
                logger.warning("Telegram single result card delivery failed", exc_info=True)
        decorated["telegram_presentation"] = {
            "poster_cards_delivered": delivered,
            "poster_count": len(cards),
            "selection_status": "single_result",
            "provider_mutation_performed": False,
            "next_action": "request the movie, or specify the desired series seasons",
            "instruction": (
                "Answer only about this result. If the current user message explicitly asks "
                "to add or request it, call the matching request tool now. Otherwise this is a "
                "read-only lookup: never imply it was requested, and if unavailable offer a "
                "clear next action ('request' for a movie, or the desired seasons for a series). "
                "The Telegram card may include a Request button that the user can tap "
                "independently; if they do, the button handler performs the request."
            ),
        }
        return decorated

    # --- Multiple results: blocking disambiguation picker ---
    actor = require_actor()
    selection = await _select_search_result(
        actor_chat_id,
        actor.user_id,
        session_key,
        choices,
        candidates,
        posters,
    )
    delivered = selection is not None
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
    if selection == MEDIA_PICKER_CANCELLED:
        decorated["results"] = []
        decorated["telegram_presentation"] = {
            "poster_cards_delivered": delivered,
            "poster_count": len(cards),
            "selection_status": "cancelled",
            "instruction": "The user cancelled the media card. End without another response.",
        }
        return decorated
    if not isinstance(selection, str):
        selection = ""
    request_status = ""
    request_failed = selection.startswith(MEDIA_PICKER_REQUEST_FAILED)
    if request_failed:
        selection = selection[len(MEDIA_PICKER_REQUEST_FAILED) :]
    elif selection.startswith(MEDIA_PICKER_REQUESTED):
        remainder = selection[len(MEDIA_PICKER_REQUESTED) :]
        request_status, _, selection = remainder.partition(":")
    if selection in choices:
        selected_index = choices.index(selection)
        selected = candidates[selected_index]
        decorated["results"] = [selected]
        if request_status:
            decorated["telegram_presentation"] = {
                "poster_cards_delivered": delivered,
                "poster_count": len(cards),
                "selection_status": "requested",
                "selected_choice": selection,
                "request_status": request_status,
                "provider_mutation_performed": True,
                "instruction": (
                    "The user's Request movie tap already performed this request through the "
                    f"gateway; its recorded status is '{request_status}'. Confirm that outcome "
                    "to the user (they are notified when it appears on Plex). Never call "
                    "request_movie for this result."
                ),
            }
            return decorated
        if request_failed:
            decorated["telegram_presentation"] = {
                "poster_cards_delivered": delivered,
                "poster_count": len(cards),
                "selection_status": "request_failed",
                "selected_choice": selection,
                "provider_mutation_performed": False,
                "instruction": (
                    "The user's Request movie tap failed inside the gateway; nothing was "
                    "requested. Tell the user it failed and that asking again will retry with "
                    "a fresh search. Do not call request_movie in this turn."
                ),
            }
            return decorated
        if selected.get("media_type") == "movie":
            instruction = (
                "The user selected this exact movie in Telegram; that selection did not itself "
                "request anything. If the current user message explicitly asks to add or request "
                "it, call request_movie now. Otherwise answer only about this result and, if "
                "unavailable, offer a clear next action. Do not repeat the candidate list."
            )
        else:
            instruction = (
                "The user selected this exact series in Telegram; that selection did not itself "
                "request anything. Answer only about this result and ask which seasons they want "
                "if a request is needed. Do not repeat the candidate list."
            )
        decorated["telegram_presentation"] = {
            "poster_cards_delivered": delivered,
            "poster_count": len(cards),
            "selection_status": "selected",
            "selected_choice": selection,
            "provider_mutation_performed": False,
            "next_action": "request the movie, or specify the desired series seasons",
            "instruction": instruction,
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
    install_tool_visibility(
        current_role=current_role,
        admin_role=Role.ADMIN,
        shared_toolset=SHARED_TOOLSET,
        admin_toolset=ADMIN_TOOLSET,
        search_toolset=SEARCH_TOOLSET,
    )


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
        session_key = require_session_key()
        if name == "search_media" and is_recommendation_turn():
            turn_text = current_turn_text()
            query = arguments.get("query")
            if _looks_like_recommendation(turn_text) or not _query_is_from_current_message(
                query, turn_text
            ):
                logger.info(
                    "Redirected single-title search to recommendation batch for session %s",
                    session_key,
                )
                return json.dumps(
                    {
                        "query": query,
                        "results": [],
                        "telegram_presentation": {
                            "poster_cards_delivered": False,
                            "selection_status": "recommendation_batch_required",
                            "instruction": (
                                "This is a recommendation turn. Choose exactly 4 distinct titles "
                                "and call recommend_media once; do not call search_media again."
                            ),
                        },
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            _clear_recommendation_context(session_key)
        result = await _gateway().call(actor, name, dict(arguments))
        if name in {"search_media", "recommend_media"} and isinstance(result.get("results"), list):
            result = await _decorate_search_result(
                actor.chat_id,
                session_key,
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
        check_fn=lambda: _NativeAdapter is not _FallbackAdapter,
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
