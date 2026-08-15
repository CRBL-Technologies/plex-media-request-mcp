"""Pinned Hermes v2026.8.3 Telegram platform override.

The extension deliberately delegates Telegram transport and message parsing to
the native Hermes adapter.  It only adds the trust boundary that ordinary
hooks cannot provide:

* a last-writer-wins ``telegram`` platform registration;
* immutable context construction from the native event/raw update;
* contextvar-scoped actor-bound typed companion wrappers; and
* interception of the dedicated ``crblc:`` callback prefix.

No generic MCP client/attachment, raw tool registry, or model-supplied actor
identity is exposed here.  The checked-in startup contract verifies that the
real pinned Hermes image loaded this registration and owns the native platform
entry before the gateway is allowed to run.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import logging
import os
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from .companion_client import (
    ADMIN_TOOLS,
    SHARED_TOOLS,
    TOOL_INVENTORY,
    CompanionAuthorizationError,
    CompanionClient,
    ConfirmationEnvelope,
    client_from_environment,
    current_policy_membership,
    policy_membership_scope,
)
from .confirmation_callback import ConfirmationCallbackHandler
from .policy_helper_api import PolicyMembership
from .trusted_context import (
    TrustedContextError,
    TrustedTelegramContext,
    require_trusted_context,
    trusted_context_from_event,
    trusted_context_from_update,
    trusted_context_scope,
)

logger = logging.getLogger(__name__)

PLUGIN_NAME = "media-policy"
PLATFORM_NAME = "telegram"
HERMES_RELEASE_DATE = "2026.8.3"
NATIVE_ADAPTER_MODULE = "plugins.platforms.telegram.adapter"
NATIVE_ADAPTER_CLASS = "TelegramAdapter"
CALLBACK_PREFIX = "crblc:"
SHARED_TOOLSET = "media-policy-shared"
ADMIN_TOOLSET = "media-policy-admin"
UPSTREAM_TOOL_CONTRACT_SHA256 = (
    "65b3b6a3d439de558ba5c1f76cc755a2f05ca57474812c765313c654b509597e"
)
# Compatibility name used by the external startup contract.  It is the
# canonical digest of the pinned upstream projection, not a live registry
# listing or MCP response.
TOOL_SCHEMA_SHA256 = UPSTREAM_TOOL_CONTRACT_SHA256
TOOL_PROFILE = "full"
TOOL_INCLUDE: tuple[str, ...] = ()
_TOOL_SCHEMA_ASSET = "tool_contract.json"

_active_adapter: ContextVar[object | None] = ContextVar(
    "hermes_media_policy_adapter", default=None
)


def _tool_schema_paths() -> tuple[Path, ...]:
    """Return only the immutable image/check-out locations for the schema asset."""

    return (
        # The image install directory is deliberately lexically after Hermes'
        # bundled ``telegram`` platform directory.  Hermes v2026.8.3 defers
        # platform imports and its deferred registry is last-writer-wins; the
        # suffix keeps this override as the final Telegram loader without
        # modifying the pinned Hermes installation.
        Path("/opt/hermes/plugins/platforms/zzzz-media-policy") / _TOOL_SCHEMA_ASSET,
        Path(__file__).resolve().parents[1]
        / "deployment/hermes/plugins/platforms/media-policy"
        / _TOOL_SCHEMA_ASSET,
    )


def _load_tool_contract() -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    """Read the signed upstream projection and companion-owned entries."""

    for path in _tool_schema_paths():
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        try:
            value = json.loads(raw.decode("utf-8", "strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("media-policy tool contract asset is invalid") from exc
        if not isinstance(value, Mapping) or value.get("schema_version") != 1:
            raise RuntimeError("media-policy tool contract version is invalid")
        upstream = value.get("upstream_tools")
        companion = value.get("companion_tools")
        if not isinstance(upstream, list) or not isinstance(companion, list):
            raise RuntimeError("media-policy tool contract lists are invalid")
        projected: list[dict[str, Any]] = []
        for entry in upstream:
            if not isinstance(entry, Mapping):
                raise RuntimeError("upstream tool contract entry is invalid")
            projected.append(
                {
                    key: entry.get(key)
                    for key in (
                        "name",
                        "title",
                        "description",
                        "inputSchema",
                        "annotations",
                    )
                }
            )
        canonical = (
            json.dumps(
                projected, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8", "strict")
            + b"\n"
        )
        if (
            value.get("upstream_source_revision")
            != "8b469d2b321b27dd1e4f5b89a7236b3ea43c3c72"
            or value.get("upstream_tool_digest")
            != "sha256:" + UPSTREAM_TOOL_CONTRACT_SHA256
            or hashlib.sha256(canonical).hexdigest() != UPSTREAM_TOOL_CONTRACT_SHA256
        ):
            raise RuntimeError("pinned upstream tool contract digest drifted")
        entries = tuple(
            cast(dict[str, Any], dict(entry))
            for entry in (*upstream, *companion)
            if isinstance(entry, Mapping)
        )
        if len(entries) != len(upstream) + len(companion):
            raise RuntimeError("media-policy tool contract entry is invalid")
        return cast(dict[str, Any], dict(value)), entries
    raise RuntimeError("media-policy tool contract asset is unavailable")


def load_frozen_tool_schemas() -> dict[str, dict[str, Any]]:
    """Load and validate the checked-in per-tool schemas.

    A missing or modified artifact is a startup failure.  In particular, a
    generic ``additionalProperties: true`` object is never accepted as a
    substitute for a reviewed tool schema.
    """

    _, entries = _load_tool_contract()
    result: dict[str, dict[str, Any]] = {}
    for entry in entries:
        name = entry.get("name")
        schema = entry.get("inputSchema")
        if not isinstance(name, str) or not isinstance(schema, Mapping):
            raise RuntimeError("media-policy tool schema entry is invalid")
        if name in result:
            raise RuntimeError("media-policy tool schema names are duplicated")
        if (
            schema.get("type") != "object"
            or not isinstance(schema.get("properties"), Mapping)
            or schema.get("additionalProperties", False) is not False
        ):
            raise RuntimeError(
                f"media-policy tool schema for {name!r} is not a closed object"
            )
        closed_schema = dict(schema)
        closed_schema["additionalProperties"] = False
        result[name] = cast(dict[str, Any], closed_schema)
    return result


def load_frozen_tool_metadata() -> dict[str, dict[str, Any]]:
    """Return the frozen title/description/annotation record per tool."""

    _, entries = _load_tool_contract()
    result: dict[str, dict[str, Any]] = {}
    for entry in entries:
        name = entry.get("name")
        if not isinstance(name, str) or name in result:
            raise RuntimeError("media-policy tool metadata is invalid")
        result[name] = entry
    return result


def frozen_tool_contract_digest() -> str:
    """Return the declared digest after validating the complete asset."""

    value, _ = _load_tool_contract()
    digest = value.get("upstream_tool_digest")
    if not isinstance(digest, str):
        raise RuntimeError("media-policy tool contract digest is missing")
    return digest


def _native_tool_visibility_patch_installed() -> bool:
    try:
        from hermes_cli import tools_config  # type: ignore[import-not-found]
    except ImportError:
        return False
    return bool(
        getattr(
            getattr(tools_config, "_get_platform_tools", None),
            "__media_policy_visibility__",
            False,
        )
    )


def _install_native_tool_visibility_patch() -> bool:
    """Make Hermes' per-turn platform toolset lookup role-aware.

    Hermes invokes ``_get_platform_tools`` immediately before constructing an
    agent, and preserves the dispatch context into its executor.  A wrapper at
    that seam gives every Telegram turn a task-local shared/admin view without
    touching the global registry's cached ``check_fn`` results.
    """

    try:
        from hermes_cli import tools_config  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - focused tests omit Hermes.
        return False
    original = getattr(tools_config, "_get_platform_tools", None)
    if not callable(original):
        return False
    if getattr(original, "__media_policy_visibility__", False):
        return True

    def role_aware_platform_tools(
        config: dict[str, Any], platform: str, *args: Any, **kwargs: Any
    ) -> object:
        resolved = original(config, platform, *args, **kwargs)
        if str(platform).lower() != PLATFORM_NAME:
            return resolved
        if not isinstance(resolved, (set, list, tuple, frozenset)):
            raise TypeError("Hermes toolset resolver returned an invalid value")
        # Hermes' native ``hermes-telegram`` default expands to the broad
        # personal-agent surface.  The media-policy platform owns the model
        # context for Telegram, so discard that default and expose exactly
        # the reviewed shared/admin toolsets below.  Other platforms retain
        # the native resolver output untouched.
        visible = {SHARED_TOOLSET}
        membership = current_policy_membership()
        if membership is not None and membership.is_admin:
            visible.add(ADMIN_TOOLSET)
        return visible

    role_aware_platform_tools.__media_policy_visibility__ = True  # type: ignore[attr-defined]
    role_aware_platform_tools.__media_policy_original__ = original  # type: ignore[attr-defined]
    tools_config._get_platform_tools = role_aware_platform_tools
    return True


class _FallbackNativeTelegramAdapter:
    """Tiny fake-compatible base used when Hermes is not installed.

    Production never uses this class: ``startup_contract`` rejects a gateway
    whose native adapter import is unavailable.  Keeping a base with the
    native method names lets focused unit tests exercise context and callback
    behavior using plain fakes.
    """

    __module__ = NATIVE_ADAPTER_MODULE

    def __init__(self, config: object) -> None:
        self.config = config

    async def handle_message(self, event: object) -> object:
        return event

    async def _handle_callback_query(self, update: object, context: object) -> object:
        return None


def _native_module() -> object | None:
    try:
        return __import__(NATIVE_ADAPTER_MODULE, fromlist=[NATIVE_ADAPTER_CLASS])
    except (ImportError, AttributeError, RuntimeError):
        return None


def _native_class() -> type:
    module = _native_module()
    candidate = (
        getattr(module, NATIVE_ADAPTER_CLASS, None) if module is not None else None
    )
    return candidate if inspect.isclass(candidate) else _FallbackNativeTelegramAdapter


_NativeTelegramAdapter = _native_class()


@dataclass(slots=True)
class _Runtime:
    client: CompanionClient
    callback: ConfirmationCallbackHandler


_runtime: _Runtime | None = None


def _membership_snapshot(
    client: CompanionClient, context: TrustedTelegramContext
) -> PolicyMembership | None:
    """Read one current helper membership for visibility sealing.

    A helper failure removes admin visibility for this turn.  Native Hermes'
    own allowlist admission still decides whether the message is processed;
    the companion wrapper independently rechecks policy before every call.
    """

    helper = getattr(client, "policy_helper", None)
    membership = getattr(helper, "membership", None)
    if not callable(membership):
        return None
    try:
        result = membership(user_id=context.user_id, chat_id=context.chat_id)
    except Exception:  # noqa: BLE001
        return None

    if isinstance(result, PolicyMembership):
        return result
    try:
        return PolicyMembership(
            user_id=context.user_id,
            chat_id=context.chat_id,
            allowed=bool(result.allowed),
            role=str(result.role),
            fingerprint=str(result.fingerprint),
            version=str(getattr(result, "version", "")),
        )
    except Exception:  # noqa: BLE001
        return None


def _confirmation_reply_markup(envelope: ConfirmationEnvelope) -> object:
    """Build one opaque ``crblc:`` Telegram callback button."""

    callback_data = envelope.callback_data
    try:
        from telegram import (  # type: ignore[import-not-found]
            InlineKeyboardButton,
            InlineKeyboardMarkup,
        )

        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("Confirm", callback_data=callback_data)]]
        )
    except ImportError:  # pragma: no cover - Hermes image has PTB installed.
        # Fakes and deployment smoke tests can inspect the Bot API shape without
        # importing python-telegram-bot.
        return {
            "inline_keyboard": [[{"text": "Confirm", "callback_data": callback_data}]]
        }


def configure_runtime(
    client: CompanionClient,
    *,
    callback_handler: ConfirmationCallbackHandler | None = None,
) -> CompanionClient:
    """Install a test/deployment runtime without changing native Hermes state."""

    global _runtime
    callback = callback_handler or ConfirmationCallbackHandler(client)
    _runtime = _Runtime(client=client, callback=callback)
    return client


def _runtime_for(config: object | None = None) -> _Runtime:
    if _runtime is not None:
        return _runtime
    # Environment construction is intentionally lazy.  Importing the plugin
    # for discovery in a developer checkout must not read a secret or require
    # a live companion; external startup validation performs the fail-closed
    # deployment check before a real gateway is started.
    client = client_from_environment()
    return _Runtime(client=client, callback=ConfirmationCallbackHandler(client))


class MediaPolicyTelegramAdapter(_NativeTelegramAdapter):  # type: ignore[misc, valid-type]
    """Native Telegram adapter with the media-policy trust boundary."""

    media_policy_override = True
    native_adapter_module = NATIVE_ADAPTER_MODULE
    native_adapter_class = NATIVE_ADAPTER_CLASS
    callback_prefix = CALLBACK_PREFIX

    def __init__(
        self,
        config: object,
        *,
        companion: CompanionClient | None = None,
        callback_handler: ConfirmationCallbackHandler | None = None,
    ) -> None:
        super().__init__(config)
        if companion is None or callback_handler is None:
            runtime = _runtime_for(config)
            companion = companion or runtime.client
            callback_handler = callback_handler or runtime.callback
        self.companion_client = companion
        self.confirmation_callback_handler = callback_handler

    async def handle_message(self, event: object) -> object:
        """Re-derive provenance at the native Hermes event boundary."""

        trusted = trusted_context_from_event(event)
        membership = _membership_snapshot(self.companion_client, trusted)
        adapter_marker = _active_adapter.set(self)
        try:
            with trusted_context_scope(trusted), policy_membership_scope(membership):
                parent = getattr(super(), "handle_message", None)
                if not callable(parent):
                    raise TrustedContextError(
                        "native Telegram adapter has no message handler"
                    )
                result = parent(event)
                if inspect.isawaitable(result):
                    return await result
                return result
        finally:
            _active_adapter.reset(adapter_marker)

    async def deliver_confirmation_preview(self, envelope: ConfirmationEnvelope) -> int:
        """Send the server-rendered preview, then bind its exact message."""

        trusted = require_trusted_context()
        bot = getattr(self, "_bot", None)
        send_message = getattr(bot, "send_message", None)
        if not callable(send_message):
            raise CompanionAuthorizationError(
                "native Telegram bot is unavailable for confirmation delivery"
            )
        reply_markup = _confirmation_reply_markup(envelope)
        try:
            result = send_message(
                chat_id=trusted.chat_id,
                text=envelope.preview,
                parse_mode=envelope.parse_mode,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            raise CompanionAuthorizationError(
                "confirmation preview could not be delivered"
            ) from exc
        message_id = (
            result.get("message_id")
            if isinstance(result, Mapping)
            else getattr(result, "message_id", None)
        )
        if (
            isinstance(message_id, bool)
            or not isinstance(message_id, int)
            or message_id <= 0
        ):
            raise CompanionAuthorizationError(
                "Telegram did not return a confirmation message ID"
            )
        try:
            bound = self.companion_client.bind_confirmation(
                token=envelope.token,
                preview=envelope.preview,
                chat_id=trusted.chat_id,
                message_id=message_id,
            )
            if inspect.isawaitable(bound):
                await bound
        except Exception as exc:
            delete = getattr(bot, "delete_message", None)
            if callable(delete):
                try:
                    cleanup = delete(
                        chat_id=trusted.chat_id,
                        message_id=message_id,
                    )
                    if inspect.isawaitable(cleanup):
                        await cleanup
                except Exception:
                    logger.debug("confirmation preview cleanup failed", exc_info=True)
            raise CompanionAuthorizationError(
                "confirmation preview could not be bound"
            ) from exc
        return message_id

    async def _handle_callback_query(self, update: object, context: object) -> object:
        """Consume ``crblc:`` before native Hermes's catch-all callback path."""

        query = (
            update.get("callback_query")
            if isinstance(update, Mapping)
            else getattr(update, "callback_query", None)
        )
        data = (
            query.get("data")
            if isinstance(query, Mapping)
            else getattr(query, "data", None)
        )
        if isinstance(data, str) and data.startswith(CALLBACK_PREFIX):
            trusted = trusted_context_from_update(update)
            with trusted_context_scope(trusted):
                return await self.confirmation_callback_handler.handle_update(
                    update, context
                )

        # Native callback handlers still run under a trusted context when they
        # were produced by a real Telegram update.  This keeps any companion
        # operation initiated by a native Hermes approval button bound to the
        # same actor, while preserving all native callback semantics.
        trusted = trusted_context_from_update(update)
        with trusted_context_scope(trusted):
            parent = getattr(super(), "_handle_callback_query", None)
            if not callable(parent):
                return None
            result = parent(update, context)
            if inspect.isawaitable(result):
                return await result
            return result

    # Explicit aliases make the binding visible to the external startup
    # contract and avoid depending on implementation details of the native
    # PTB handler registration.
    async def handle_confirmation_callback(
        self, update: object, context: object
    ) -> object:
        return await self._handle_callback_query(update, context)


def _native_requirements() -> bool:
    module = _native_module()
    checker = (
        getattr(module, "check_telegram_requirements", None)
        if module is not None
        else None
    )
    if checker is None:
        return module is not None
    try:
        return bool(checker())
    except (ImportError, AttributeError, RuntimeError):
        return False


def _validate_config(config: object) -> bool:
    token = getattr(config, "token", None)
    if token is None:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    return isinstance(token, str) and bool(token.strip())


def _is_connected(config: object) -> bool:
    return _validate_config(config)


def _build_adapter(config: object) -> MediaPolicyTelegramAdapter:
    runtime = _runtime_for(config)
    return MediaPolicyTelegramAdapter(
        config,
        companion=runtime.client,
        callback_handler=runtime.callback,
    )


def _tool_handler(tool: str):
    async def handler(arguments: Mapping[str, Any]) -> dict[str, Any]:
        runtime = _runtime_for()
        result = await runtime.client.call_tool_async(tool, arguments)
        if result.confirmation is not None:
            adapter = _active_adapter.get()
            deliver = getattr(adapter, "deliver_confirmation_preview", None)
            if not callable(deliver):
                raise CompanionAuthorizationError(
                    "native Telegram adapter is unavailable for confirmation delivery"
                )
            await deliver(result.confirmation)
        return result.to_dict()

    return handler


def _register_tools(ctx: object) -> tuple[str, ...]:
    register = getattr(ctx, "register_tool", None)
    if not callable(register):
        raise TypeError("Hermes plugin context lacks typed tool registration")
    schemas = load_frozen_tool_schemas()
    metadata = load_frozen_tool_metadata()
    if set(schemas) != set(TOOL_INVENTORY):
        raise RuntimeError("media-policy tool schema inventory drifted")
    if set(metadata) != set(TOOL_INVENTORY):
        raise RuntimeError("media-policy tool metadata inventory drifted")
    for tool in TOOL_INVENTORY:
        schema = schemas.get(tool)
        record = metadata.get(tool)
        if schema is None or record is None:
            raise RuntimeError(f"media-policy schema is missing for {tool!r}")
        description = record.get("description")
        if not isinstance(description, str) or not description.strip():
            raise RuntimeError(f"media-policy description is missing for {tool!r}")
        register(
            name=tool,
            toolset=(SHARED_TOOLSET if tool in SHARED_TOOLS else ADMIN_TOOLSET),
            schema=copy.deepcopy(schema),
            handler=_tool_handler(tool),
            is_async=True,
            description=description,
            emoji="🎬",
        )
    return TOOL_INVENTORY


def register(ctx: object) -> None:
    """Hermes plugin entry point.

    The platform registration intentionally uses the native name ``telegram``
    so Hermes's registry last-writer-wins behavior replaces the pinned native
    entry.  We do not register an MCP server, a wildcard tool, or a generic
    callback hook.
    """

    _install_native_tool_visibility_patch()
    register_platform = getattr(ctx, "register_platform", None)
    if not callable(register_platform):
        raise TypeError("Hermes plugin context lacks platform registration")
    register_platform(
        name=PLATFORM_NAME,
        label="Telegram (media policy)",
        adapter_factory=_build_adapter,
        check_fn=_native_requirements,
        validate_config=_validate_config,
        is_connected=_is_connected,
        required_env=["TELEGRAM_BOT_TOKEN"],
        install_hint="Hermes v2026.8.3 native Telegram dependencies are required.",
        allowed_users_env="TELEGRAM_ALLOWED_USERS",
        allow_all_env="TELEGRAM_ALLOW_ALL_USERS",
        plugin_name=PLUGIN_NAME,
        emoji="✈️",
        max_message_length=4096,
        allow_update_command=True,
        platform_hint="Telegram identity is supplied by native Hermes; never ask the user for Telegram IDs.",
    )
    _register_tools(ctx)


__all__ = [
    "ADMIN_TOOLS",
    "ADMIN_TOOLSET",
    "CALLBACK_PREFIX",
    "HERMES_RELEASE_DATE",
    "NATIVE_ADAPTER_CLASS",
    "NATIVE_ADAPTER_MODULE",
    "PLATFORM_NAME",
    "PLUGIN_NAME",
    "SHARED_TOOLS",
    "SHARED_TOOLSET",
    "TOOL_SCHEMA_SHA256",
    "TOOL_INVENTORY",
    "MediaPolicyTelegramAdapter",
    "configure_runtime",
    "frozen_tool_contract_digest",
    "load_frozen_tool_schemas",
    "load_frozen_tool_metadata",
    "register",
]
