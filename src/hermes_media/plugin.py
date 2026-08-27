"""Minimal Hermes v2026.8.3 Telegram adapter and role-aware tools."""

from __future__ import annotations

import asyncio
import html
import inspect
import json
import logging
import os
from collections.abc import Callable, Coroutine, Mapping
from concurrent.futures import Future
from typing import Any
from urllib.parse import urlsplit

from media_gateway.constants import ADMIN_UPSTREAM_TOOLS, SHARED_TOOLS
from media_gateway.types import Role

from .client import GatewayClient
from .compat import (
    install_platform_hint,
    install_tool_visibility,
    native_adapter,
    send_single_result_card,
)
from .trusted import (
    actor_from_event,
    actor_scope,
    claim_card_slot,
    current_role,
    require_actor,
    session_key_from_event,
)

PLATFORM = "telegram"
SHARED_TOOLSET = "crbl-media-shared"
ADMIN_TOOLSET = "crbl-media-admin"
SEARCH_TOOLSET = "search"
WEB_SEARCH_CAP = 10
CAPTION_HEADING_LIMIT = 200
CAPTION_OVERVIEW_LIMIT = 420
PLATFORM_HINT = (
    "Telegram identity is trusted automatically; never ask for user IDs. "
    "The media tools are the only source of truth for this library: what is in it, what is "
    "missing, and which seasons exist. Never answer that from memory, from your own knowledge, "
    "or from results earlier in the conversation. Anything the library cannot answer -- what a "
    "film is about, what exists, what order to watch things in -- is yours to answer or to "
    "research with web_search. "
    "Adding to the library is the only thing you can change here, so do it when asked and never "
    "report something as requested unless a tool said so. "
    "A single resolved media result is also posted to the chat as a poster, carrying a link "
    "that opens "
    "Plex when the title is held; at most one poster is posted per message, so do not describe "
    "the card or promise buttons. Choose the tools yourself."
)


logger = logging.getLogger(__name__)


class _FallbackAdapter:
    def __init__(self, config: object) -> None:
        self.config = config

    async def handle_message(self, event: object) -> object:
        return event


_NativeAdapter = native_adapter(_FallbackAdapter)
_client: GatewayClient | None = None


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
        with actor_scope(
            actor,
            role,
            session_key,
        ):
            result = super().handle_message(event)
            return await result if inspect.isawaitable(result) else result


_active_adapter: MediaTelegramAdapter | None = None


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


def _search_presentation(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Keep every row the model can name, in the order the gateway ranked them.

    A row without a usable label cannot be named or requested, so it is
    dropped. Nothing else is: recommend_media takes up to twenty titles and
    search_media a limit of ten, and a title cut here would be invisible --
    ``unmatched_titles`` reports only what the providers failed to match, so a
    truncated one looks exactly like a title that was never asked for.
    """

    rows = result.get("results")
    if not isinstance(rows, list):
        return []
    return [dict(row) for row in rows if _candidate_label(row) is not None]


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


async def _decorate_search_result(
    actor_chat_id: int,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Deliver a poster for a lone result and hand everything else to the model.

    There is no picker. A card can only ever offer a link to something already
    on Plex, so every choice -- which of several matches was meant, whether to
    add a missing title, which seasons of a series -- is made by talking to the
    model, which can ask and be answered in the same breath.
    """

    recommendation_mode = result.get("presentation") == "recommendations"
    candidates = _search_presentation(result)
    if not candidates:
        return result
    decorated = dict(result)
    decorated["results"] = candidates

    if recommendation_mode and len(candidates) > 1:
        decorated["telegram_presentation"] = {
            "poster_cards_delivered": False,
            "selection_status": "conversational",
            "instruction": (
                "Present these recommendations conversationally. For each title, state its "
                "name, year, media type, and whether it is on Plex. Offer to add any that are "
                "missing. Do not repeat raw candidate data."
            ),
        }
        return decorated

    if len(candidates) > 1:
        decorated["telegram_presentation"] = {
            "poster_cards_delivered": False,
            "selection_status": "conversational",
            "instruction": (
                "Several titles match. List them in your reply with year, media type and "
                "whether each is on Plex, and ask which one the user means. Never claim a "
                "selection was made, and request nothing until they answer."
            ),
        }
        return decorated

    delivered = False
    adapter = _active_adapter
    candidate = candidates[0]
    # A card is unsolicited, so one message earns one. A model that searches
    # several titles in a turn must not post a poster for each.
    if adapter is not None and claim_card_slot():
        # ``is_safe_url`` performs a blocking DNS resolve, so it runs here
        # rather than on the adapter loop, and only once a card is certain to
        # be sent -- validating twenty posters to show at most one was work
        # spent on nothing.
        poster = _safe_poster_url(candidate.get("poster_url"))

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
    # Only one card is sent per message and delivery can fail, so the second
    # lookup in a turn has no poster to point at. Saying otherwise invites a
    # reply that refers the user to a card they were never shown.
    card = (
        "A poster for this result was already sent, carrying an Open in Plex link when "
        "it is available. "
        if delivered
        else ""
    )
    decorated["telegram_presentation"] = {
        "poster_cards_delivered": delivered,
        "selection_status": "single_result",
        "provider_mutation_performed": False,
        "instruction": (
            f"{card}Answer only about this result. If this is a recommendation, explain why "
            "it fits the user's question. If the current user message "
            "explicitly asks to add or request it, call the matching request tool now; a "
            "series still needs its seasons named. Otherwise this is a read-only lookup: "
            "never imply it was requested."
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


def _platform_hint_patch() -> None:
    install_platform_hint(platform=PLATFORM, platform_hint=PLATFORM_HINT)


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


def _handler(name: str) -> Callable[..., Coroutine[Any, Any, str]]:
    async def call(arguments: Mapping[str, Any], **runtime: Any) -> str:
        del runtime
        actor = require_actor()
        result = await _gateway().call(actor, name, dict(arguments))
        if name in {"search_media", "recommend_media"} and isinstance(result.get("results"), list):
            result = await _decorate_search_result(actor.chat_id, result)
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
    # PLATFORM_HINT is the only copy of this text. Hermes prefers its built-in
    # Telegram hint over the value registered below, so the guidance is
    # installed on the resolver instead of being mirrored into config.yaml.
    _platform_hint_patch()
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
