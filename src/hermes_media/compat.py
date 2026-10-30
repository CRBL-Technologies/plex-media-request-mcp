"""All compatibility code for the pinned Hermes v0.22.5 runtime."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable, Mapping
from types import SimpleNamespace
from typing import Any

from media_gateway.policy import Policy
from media_gateway.types import Actor, Role

from .trusted import actor_for_turn, actor_from_event

PINNED_HERMES_RELEASE = "v0.22.5"
PINNED_HERMES_COMMIT = "bf0203a794a07aac3ff764972ab9087baa4e4f4e"
NATIVE_MODULE = "plugins.platforms.telegram.adapter"
NATIVE_CLASS = "TelegramAdapter"

logger = logging.getLogger(__name__)


def _policy_allows(user_id: str) -> bool:
    """Read the live policy under the native caller's transport-profile scope."""

    from hermes_constants import get_hermes_home  # type: ignore[import-not-found]

    if not isinstance(user_id, str) or not user_id.isascii() or not user_id.isdecimal():
        return False
    try:
        return Policy(get_hermes_home() / ".env").snapshot().role(int(user_id)) is not Role.BLOCKED
    except (OSError, ValueError):
        return False


def isolate_actor_sessions(adapter: Any) -> None:
    """Keep native queue keys and the runner's history keys isolated together."""

    runner = getattr(adapter, "gateway_runner", None)
    store = getattr(runner, "session_store", None)
    original = getattr(store, "_generate_session_key", None)
    resolve_profile = getattr(store, "_resolve_profile_for_key", None)
    extra = getattr(adapter.config, "extra", None)
    if (
        store is None
        or not callable(original)
        or not callable(resolve_profile)
        or not isinstance(extra, dict)
    ):
        raise RuntimeError("Hermes session isolation boundary is unavailable")
    for key in ("group_sessions_per_user", "thread_sessions_per_user"):
        extra[key] = True
    if getattr(original, "__crbl_media_isolation__", False):
        return

    def session_key(source: Any, key_source: Any = None) -> str:
        platform = getattr(source.platform, "value", source.platform)
        if platform != "telegram":
            return str(original(source, key_source))
        from gateway.session import build_session_key  # type: ignore[import-not-found]

        return str(
            build_session_key(
                key_source if key_source is not None else source,
                group_sessions_per_user=True,
                thread_sessions_per_user=True,
                profile=resolve_profile(source),
            )
        )

    session_key.__crbl_media_isolation__ = True  # type: ignore[attr-defined]
    # Runner and async store paths both delegate to this canonical store method.
    # Do not change global flags: other chat platforms may share the same runner.
    store._generate_session_key = session_key


def preserve_handler_owner(adapter: Any, handler: Callable[[Any], Any]) -> None:
    """Preserve native runner ownership on the wrapped message handler."""

    handler.__self__ = adapter.gateway_runner  # type: ignore[attr-defined]


def install_runner_boundary(adapter: Any) -> None:
    """Cover native persisted replay, which bypasses the adapter handler."""

    runner = adapter.gateway_runner
    principal = getattr(runner, "_principal_authorized", None)
    execute = getattr(runner, "_handle_message", None)
    if not callable(principal) or not callable(execute):
        raise RuntimeError("Hermes runner authorization boundary is unavailable")
    if getattr(principal, "__crbl_media_policy__", False):
        return

    def current_adapter(source: Any) -> Any:
        if getattr(source.platform, "value", source.platform) != "telegram":
            return None
        current = runner._adapter_for_source(source)
        # Resolve at use time: an adapter can be replaced after registration.
        return current if isinstance(current, type(adapter)) else None

    def authorize(source: Any, *, allow_adapter_delegation: bool) -> bool:
        current = current_adapter(source)
        if current is None:
            return bool(principal(source, allow_adapter_delegation=allow_adapter_delegation))
        # Replace only the principal verdict; native bot-loop protection remains.
        return _policy_allows(source.user_id)

    async def dispatch(event: Any) -> Any:
        current = current_adapter(event.source)
        if current is None:
            return await execute(event)
        if not runner._is_user_authorized_for_source(event.source):
            raise PermissionError("Telegram user is not allowed")
        return await current._execute_media_turn(event, execute)

    authorize.__crbl_media_policy__ = True  # type: ignore[attr-defined]
    runner._principal_authorized = authorize
    runner._handle_message = dispatch


def execution_actor(adapter: Any, event: Any) -> Actor:
    """Accept restored identity only inside native admitted execution."""

    if getattr(event, "raw_message", None) is None:
        try:
            from gateway.session_ingress import (  # type: ignore[import-not-found]
                executing_admission,
            )
        except ImportError:
            return actor_for_turn(event)

        source = event.source
        transport = getattr(source, "_transport_adapter_ref", None)
        if executing_admission.get() is True and callable(transport) and transport() is adapter:
            # Native envelope restoration verifies the persisted source and its
            # current transport binding. Reuse the ordinary strict ID checks.
            try:
                sender = SimpleNamespace(id=int(source.user_id))
                chat = SimpleNamespace(id=int(source.chat_id))
            except (TypeError, ValueError) as error:
                raise PermissionError("Invalid admitted Telegram identity") from error
            return actor_from_event(
                SimpleNamespace(
                    source=source, raw_message=SimpleNamespace(from_user=sender, chat=chat)
                )
            )
    return actor_for_turn(event)


def mark_authorized_event(event: Any) -> None:
    """Forward the gateway's completed authorization for this message only."""

    event.source.role_authorized = True


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

    class IsolatedTelegramAdapter(candidate):  # type: ignore[misc, valid-type]
        def _telegram_observe_unmentioned_group_messages(self) -> bool:
            # Native observation erases sender IDs. No environment override can
            # enable that shared history in this per-user policy adapter.
            return False

        def _is_callback_user_authorized(
            self,
            user_id: str,
            *,
            chat_id: str | None = None,
            chat_type: str | None = None,
            thread_id: str | None = None,
            user_name: str | None = None,
        ) -> bool:
            # The injected check enters the receiving bot's transport profile.
            # Its runner principal is wrapped above, so it reads the live file
            # instead of falling back to stale environment grants.
            if getattr(self, "_authorization_check", None) is not None:
                return (
                    self._is_sender_authorized(
                        user_id,
                        chat_type=self._normalize_chat_type(
                            chat_type, is_forum=thread_id is not None
                        ),
                        chat_id=str(chat_id or user_id),
                        thread_id=str(thread_id) if thread_id is not None else None,
                    )
                    is True
                )
            if getattr(self, "_owner_profile", None):
                # A secondary bot must never borrow the ambient primary policy.
                return False
            return _policy_allows(user_id)

        def _media_picker_owned(self, query: Any, state: Any, chat_id: str) -> bool:
            message = query.message
            if not isinstance(state, dict) or message is None:
                return False
            message_id = getattr(message, "message_id", None)
            if not isinstance(message_id, int) or isinstance(message_id, bool):
                return False
            if state.get("msg_id") != message_id or not state.get("session_key"):
                return False
            source = self.build_source(
                chat_id=chat_id,
                user_id=str(query.from_user.id),
                chat_type=self._normalize_chat_type(
                    message.chat.type, is_forum=bool(getattr(message.chat, "is_forum", False))
                ),
                thread_id=getattr(message, "message_thread_id", None),
            )
            return bool(state["session_key"] == self._source_session_key(source))

        async def _handle_model_picker_callback(self, query: Any, data: str, chat_id: str) -> None:
            if not self._media_picker_owned(query, self._model_picker_state.get(chat_id), chat_id):
                await query.answer(text="This control belongs to another or expired session.")
                return
            await super()._handle_model_picker_callback(query, data, chat_id)

        async def _handle_choice_picker_callback(self, query: Any, data: str, chat_id: str) -> None:
            if not self._media_picker_owned(query, self._choice_picker_state.get(chat_id), chat_id):
                await query.answer(text="This control belongs to another or expired session.")
                return
            await super()._handle_choice_picker_callback(query, data, chat_id)

    return IsolatedTelegramAdapter


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


def native_telegram_hint() -> str:
    """Read the formatting guidance shipped by the pinned Telegram plugin."""

    try:
        from plugins.platforms.telegram.adapter_hint import (  # type: ignore[import-not-found]
            PLATFORM_HINT,
        )
    except ImportError:
        return ""
    return str(PLATFORM_HINT)


def install_platform_hint(*, platform: str, platform_hint: str) -> None:
    """Append the CRBL guidance to the resolved prompt for one platform.

    The registered hint includes native formatting and CRBL guidance. This
    wrapper keeps CRBL guidance present when a user config override replaces
    that default. The override still applies and is not silently discarded.
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
    event: object | None = None,
) -> object:
    """Send one resolved media result with an optional Plex button."""

    bot = getattr(adapter, "_bot", None)
    if bot is None:
        return SimpleNamespace(success=False)
    markup = _single_result_markup(candidate=candidate)
    routing: dict[str, Any] = {}
    if event is not None:
        # Use the same pinned-runtime topic and DM-anchor rules as native replies.
        # Keep these private dependencies here, not in the tool or identity layer.
        from gateway.platforms.base import (  # type: ignore[import-not-found]
            _reply_anchor_for_event,
            _thread_metadata_for_source,
        )

        native: Any = adapter
        metadata = _thread_metadata_for_source(
            getattr(event, "source", None), _reply_anchor_for_event(event)
        )
        mode = native._reply_to_mode
        reply_id = native._reply_to_message_id_for_send(
            _reply_anchor_for_event(event), metadata, reply_to_mode=mode
        )
        routing = native._thread_kwargs_for_send(
            str(chat_id),
            native._metadata_thread_id(metadata),
            metadata,
            reply_to_message_id=reply_id,
            reply_to_mode=mode,
        )
        if reply_id is not None:
            routing["reply_to_message_id"] = reply_id
    if poster_url is not None:
        message = await bot.send_photo(
            chat_id=chat_id,
            photo=poster_url,
            caption=caption,
            parse_mode="HTML",
            reply_markup=markup,
            **routing,
        )
    else:
        message = await bot.send_message(
            chat_id=chat_id,
            text=caption,
            parse_mode="HTML",
            reply_markup=markup,
            disable_web_page_preview=True,
            **routing,
        )
    return SimpleNamespace(success=True, message_id=str(message.message_id))


def verify_pinned_runtime(
    *, manager: object, expected_tools: set[str], platform_hint: str, soul: str
) -> None:
    """Fail closed when any private pinned-Hermes integration contract moves."""

    from agent import system_prompt  # type: ignore[import-not-found,unused-ignore]
    from agent.prompt_builder import (  # type: ignore[import-not-found]
        load_soul_md,
    )
    from agent.web_search_registry import (  # type: ignore[import-not-found]
        get_active_search_provider,
    )
    from gateway.platform_registry import platform_registry  # type: ignore[import-not-found]
    from hermes_cli import tools_config
    from hermes_cli.version_info import get_version_info  # type: ignore[import-not-found]
    from toolsets import TOOLSETS  # type: ignore[import-not-found]

    version = get_version_info()
    if (
        version.display_version != PINNED_HERMES_RELEASE.removeprefix("v")
        or version.commit != PINNED_HERMES_COMMIT
    ):
        raise RuntimeError("pinned Hermes build identity changed")
    if native_adapter(SimpleNamespace) is SimpleNamespace:
        raise RuntimeError("native Hermes Telegram adapter is unavailable")
    provider = get_active_search_provider()
    if provider is None or provider.name != "ddgs" or not provider.is_available():
        raise RuntimeError("DuckDuckGo search provider is unavailable")
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
    native_hint = native_telegram_hint()
    registered_hint = getattr(entry, "platform_hint", "")
    if not native_hint or native_hint not in registered_hint:
        raise RuntimeError("native Telegram formatting guidance is absent from the registered hint")
    # Resolve with no config override at all: the guidance must reach the
    # assembled prompt from the code constant alone, or the single source of
    # truth is not actually installed.
    effective = resolve_hint(
        SimpleNamespace(_platform_hint_overrides={}),
        "telegram",
        registered_hint,
    )
    if platform_hint not in effective:
        raise RuntimeError("CRBL media guidance is absent from the effective Telegram prompt")
    # The built-in hint must survive alongside it; replacing Hermes' own
    # Telegram formatting guidance would break MarkdownV2 output.
    if native_hint not in effective:
        raise RuntimeError("CRBL guidance replaced rather than extended the Telegram hint")
    # The identity slot is an ordinary file read from HERMES_HOME, so a skipped
    # install, a host copy edited by hand and a context limit that truncates the
    # file all present identically: the agent serves without the identity it was
    # built with. Read it back through Hermes' own loader, which is the only
    # reader whose answer matters.
    identity = load_soul_md()
    if identity is None or soul.strip() not in identity:
        raise RuntimeError("CRBL SOUL.md is not the identity Hermes loads")
