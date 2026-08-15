"""Actor-bound, typed companion client used by the Hermes platform override.

This is deliberately a wrapper client, not a generic MCP attachment.  The
only callable operation is ``call_tool`` and it accepts names from the frozen
shared/admin inventories.  Every call obtains the immutable native Telegram
context from the context variable, rechecks current policy through the typed
helper, and injects one short-lived ``X-CRBL-Actor`` assertion after the model
arguments are available.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import math
import os
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

from .policy_helper_api import PolicyHelperAPI, PolicyHelperDenied, PolicyMembership
from .trusted_context import (
    TrustedContextError,
    TrustedTelegramContext,
    require_trusted_context,
)

_tool_policy: Any = None
try:  # Hermes-focused unit tests may omit the companion package entirely.
    _tool_policy = importlib.import_module("media_companion.tool_policy")
except ImportError:  # pragma: no cover - exercised by Hermes-only discovery.
    pass


MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
DEFAULT_TIMEOUT = (3.0, 15.0)
COMPANION_AUDIENCE = "media-companion"
ACTOR_HEADER = "X-CRBL-Actor"
PRIVATE_CONFIRM_BIND_ENDPOINT = "/private/confirm/bind"
PRIVATE_CONFIRM_CALLBACK_ENDPOINT = "/private/confirm/callback"
MAX_CONFIRMATION_PREVIEW_BYTES = 64 * 1024
DEFAULT_POLICY_HELPER_URL = "http://127.0.0.1:8787"
_CONFIRMATION_TOKEN_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
)
_CONFIRMATION_PARSE_MODES = frozenset({"HTML", "Markdown", "MarkdownV2", ""})


@dataclass(frozen=True, slots=True)
class ConfirmationEnvelope:
    """Server-rendered admin preview awaiting a native Telegram bind.

    The plaintext token is retained only in this short-lived in-process value
    long enough to post the exact preview and bind its Telegram message.  It
    is intentionally excluded from :meth:`CompanionResult.to_dict`, which is
    the model-facing representation.
    """

    token: str
    preview: str
    expires_at: int | None = None
    parse_mode: str = "HTML"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.token, str)
            or len(self.token) != 43
            or any(
                character not in _CONFIRMATION_TOKEN_CHARS for character in self.token
            )
        ):
            raise CompanionProtocolError("confirmation token is invalid")
        if not isinstance(self.preview, str) or not self.preview.strip():
            raise CompanionProtocolError("confirmation preview is invalid")
        if len(self.preview.encode("utf-8", "strict")) > MAX_CONFIRMATION_PREVIEW_BYTES:
            raise CompanionProtocolError("confirmation preview is too large")
        if self.expires_at is not None and (
            isinstance(self.expires_at, bool)
            or not isinstance(self.expires_at, int)
            or self.expires_at <= 0
        ):
            raise CompanionProtocolError("confirmation expiry is invalid")
        if (
            not isinstance(self.parse_mode, str)
            or self.parse_mode not in _CONFIRMATION_PARSE_MODES
        ):
            raise CompanionProtocolError("confirmation parse mode is invalid")

    @property
    def callback_data(self) -> str:
        """Return the only callback payload allowed on a Telegram button."""

        return "crblc:" + self.token


_sealed_policy_membership: ContextVar[PolicyMembership | None] = ContextVar(
    "hermes_media_policy_membership", default=None
)


@contextmanager
def policy_membership_scope(
    membership: PolicyMembership | None,
) -> Iterator[PolicyMembership | None]:
    """Seal one helper snapshot for toolset visibility in this dispatch."""

    marker = _sealed_policy_membership.set(membership)
    try:
        yield membership
    finally:
        _sealed_policy_membership.reset(marker)


def current_policy_membership() -> PolicyMembership | None:
    """Return the task-local helper snapshot, if a native dispatch sealed one."""

    return _sealed_policy_membership.get()


def _inventory() -> tuple[tuple[str, ...], tuple[str, ...]]:
    if _tool_policy is None:
        # The fallback is intentionally only the safe shared surface.  A real
        # deployment must ship the pinned media_companion policy module; the
        # startup contract rejects an incomplete inventory before gateway run.
        return (
            (
                "search_media",
                "request_movie",
                "request_series",
                "request_status",
                "download_status",
                "browse_library",
                "media_status",
            ),
            (),
        )
    return tuple(_tool_policy.SHARED_TOOLS), tuple(_tool_policy.ADMIN_TOOLS)


SHARED_TOOLS, ADMIN_TOOLS = _inventory()
TOOL_POLICY_AVAILABLE = _tool_policy is not None
TOOL_INVENTORY: tuple[str, ...] = SHARED_TOOLS + ADMIN_TOOLS
TOOL_SET = frozenset(TOOL_INVENTORY)
SHARED_TOOL_SET = frozenset(SHARED_TOOLS)
ADMIN_TOOL_SET = frozenset(ADMIN_TOOLS)


class CompanionError(RuntimeError):
    """Base class for safe companion-boundary failures."""


class CompanionUnavailable(CompanionError):
    """The companion cannot be reached or is not configured."""


class CompanionAuthorizationError(CompanionError):
    """No current policy decision permits the requested tool."""


class CompanionToolDenied(CompanionError):
    """The requested name is not part of the frozen wrapper inventory."""


class CompanionProtocolError(CompanionError):
    """The companion returned an invalid/oversized typed response."""


class CompanionTransport(Protocol):
    def request(self, method: str, url: str, **kwargs: Any) -> object: ...


def _safe_args(arguments: object) -> dict[str, Any]:
    if not isinstance(arguments, Mapping):
        raise TypeError("tool arguments must be an object")
    try:
        encoded = json.dumps(dict(arguments), ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise CompanionToolDenied("tool arguments are not bounded JSON") from exc
    raw = encoded.encode("utf-8", "strict")
    if len(raw) > MAX_REQUEST_BYTES:
        raise CompanionToolDenied("tool arguments exceed the request bound")
    # Round-trip to reject NaN/Infinity and duplicate/untyped values before
    # signing.  The signer then canonicalizes the exact same object.
    try:
        value = json.loads(
            encoded, parse_constant=lambda _: (_ for _ in ()).throw(ValueError())
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CompanionToolDenied("tool arguments are not valid JSON") from exc
    if not isinstance(value, dict):
        raise CompanionToolDenied("tool arguments must be an object")
    return cast(dict[str, Any], value)


def _header(headers: Mapping[object, object], name: str) -> str | None:
    wanted = name.lower()
    values = [
        str(value) for key, value in headers.items() if str(key).lower() == wanted
    ]
    if len(values) != 1:
        return values[0] if values else None
    return values[0]


@dataclass(frozen=True, slots=True)
class CompanionResult:
    """Bounded typed result returned by one registered wrapper."""

    tool: str
    content: tuple[str, ...] = ()
    structured_content: dict[str, Any] | None = None
    is_error: bool = False
    confirmation: ConfirmationEnvelope | None = None

    @property
    def text(self) -> str:
        return "\n".join(self.content)

    @property
    def structuredContent(self) -> dict[str, Any] | None:
        return self.structured_content

    def to_dict(self) -> dict[str, Any]:
        # A model must never receive a token or the server-rendered preview.
        # The native Telegram adapter consumes ``confirmation`` before this
        # representation is returned from the tool handler.
        if self.confirmation is not None:
            return {
                "tool": self.tool,
                "content": [
                    {
                        "type": "text",
                        "text": "Confirmation requested in Telegram. Use the button shown there.",
                    }
                ],
                "is_error": self.is_error,
                "structuredContent": {"confirmation_required": True},
            }
        value: dict[str, Any] = {
            "tool": self.tool,
            "content": [{"type": "text", "text": item} for item in self.content],
            "is_error": self.is_error,
        }
        if self.structured_content is not None:
            value["structuredContent"] = self.structured_content
        return value


def _bounded_value(value: object, *, depth: int = 0) -> Any:
    if depth > 16:
        raise CompanionProtocolError("companion result is too deeply nested")
    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, float) and not math.isfinite(value):
            raise CompanionProtocolError(
                "companion result contains a non-finite number"
            )
        if (
            isinstance(value, str)
            and len(value.encode("utf-8", "strict")) > MAX_RESPONSE_BYTES
        ):
            raise CompanionProtocolError("companion result contains oversized text")
        return value
    if isinstance(value, Mapping):
        if len(value) > 512:
            raise CompanionProtocolError("companion result contains too many fields")
        result: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str) or len(key.encode("utf-8", "strict")) > 512:
                raise CompanionProtocolError("companion result has an invalid field")
            result[key] = _bounded_value(child, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > 5_000:
            raise CompanionProtocolError("companion result contains too many items")
        return [_bounded_value(child, depth=depth + 1) for child in value]
    raise CompanionProtocolError("companion result contains an unsupported value")


def _decode_result(tool: str, response: object) -> CompanionResult:
    if isinstance(response, Mapping):
        payload: object = response
    else:
        status = getattr(response, "status_code", 200)
        try:
            status_int = int(status)
        except (TypeError, ValueError):
            status_int = 500
        if status_int < 200 or status_int >= 300:
            # The numeric status is safe operational context and is essential
            # for distinguishing authentication/policy failures from service
            # outages. Never include the response body here: it may contain
            # provider-controlled or sensitive diagnostic text.
            raise CompanionUnavailable(f"companion returned HTTP {status_int}")
        body = getattr(response, "content", None)
        if body is None:
            body = getattr(response, "text", None)
        if isinstance(body, str):
            body = body.encode("utf-8", "strict")
        if not isinstance(body, (bytes, bytearray, memoryview)):
            raise CompanionProtocolError("companion response body is invalid")
        raw = bytes(body)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise CompanionProtocolError("companion response is too large")
        try:
            payload = json.loads(raw.decode("utf-8", "strict"))
        except (UnicodeDecodeError, ValueError, TypeError) as exc:
            raise CompanionProtocolError("companion response is not JSON") from exc
    if not isinstance(payload, Mapping):
        raise CompanionProtocolError("companion response is not an object")
    if payload.get("error") is not None:
        # Do not echo a provider/error body that might contain credentials or
        # model-controlled text.
        raise CompanionProtocolError("companion rejected the typed tool call")
    result: object = payload.get("result", payload)
    if not isinstance(result, Mapping):
        raise CompanionProtocolError("companion result is not an object")
    content_value = result.get("content", ())
    if not isinstance(content_value, (list, tuple)):
        raise CompanionProtocolError("companion result content is not typed")
    content: list[str] = []
    for item in content_value:
        if (
            not isinstance(item, Mapping)
            or item.get("type") != "text"
            or not isinstance(item.get("text"), str)
        ):
            raise CompanionProtocolError(
                "companion result contains unsupported content"
            )
        content.append(str(item["text"]))
    structured = result.get("structuredContent", result.get("structured_content"))
    if structured is not None:
        if not isinstance(structured, Mapping):
            raise CompanionProtocolError("companion structured result is not an object")
        structured_obj = cast(dict[str, Any], _bounded_value(structured))
    else:
        structured_obj = None
    confirmation = _decode_confirmation(result.get("confirmation"))
    is_error = result.get("isError", result.get("is_error", False))
    if not isinstance(is_error, bool):
        raise CompanionProtocolError("companion error flag is not typed")
    return CompanionResult(
        tool=tool,
        content=tuple(content),
        structured_content=structured_obj,
        is_error=is_error,
        confirmation=confirmation,
    )


def _decode_confirmation(value: object) -> ConfirmationEnvelope | None:
    """Decode one narrow server confirmation envelope, failing closed."""

    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise CompanionProtocolError("confirmation envelope is not typed")
    token = value.get("token", value.get("value"))
    preview = value.get("preview", value.get("text"))
    expires_at = value.get("expires_at", value.get("expiresAt"))
    parse_mode = value.get("parse_mode", value.get("parseMode", "HTML"))
    if expires_at is not None and (
        isinstance(expires_at, bool) or not isinstance(expires_at, int)
    ):
        raise CompanionProtocolError("confirmation expiry is invalid")
    return ConfirmationEnvelope(
        token=cast(str, token),
        preview=cast(str, preview),
        expires_at=expires_at,
        parse_mode=cast(str, parse_mode),
    )


def _validate_confirmation_token(token: object) -> str:
    if (
        not isinstance(token, str)
        or len(token) != 43
        or any(character not in _CONFIRMATION_TOKEN_CHARS for character in token)
    ):
        raise CompanionProtocolError("confirmation token is invalid")
    return token


def _validate_positive_id(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CompanionProtocolError(f"confirmation {name} is invalid")
    return value


def _validate_chat_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value == 0:
        raise CompanionProtocolError("confirmation chat ID is invalid")
    return value


class CompanionClient:
    """Typed, actor-bound client with no arbitrary MCP/raw call method."""

    def __init__(
        self,
        base_url: str | None,
        *,
        signer: object,
        policy_helper: PolicyHelperAPI | object | None,
        transport: CompanionTransport | Callable[..., object] | None = None,
        timeout: tuple[float, float] = DEFAULT_TIMEOUT,
        endpoint_suffix: str = "/mcp",
    ) -> None:
        if base_url is not None:
            if not isinstance(base_url, str) or not base_url.strip():
                raise ValueError("companion URL is invalid")
            parsed = urlsplit(base_url.strip())
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("companion URL is invalid")
            try:
                _ = parsed.port
            except ValueError as exc:
                raise ValueError("companion URL is invalid") from exc
            base_url = base_url.rstrip("/")
        if (
            not isinstance(endpoint_suffix, str)
            or not endpoint_suffix.startswith("/")
            or "?" in endpoint_suffix
            or "#" in endpoint_suffix
        ):
            raise ValueError("companion endpoint is invalid")
        if (
            len(timeout) != 2
            or timeout[0] <= 0
            or timeout[1] <= 0
            or timeout[0] > 3
            or timeout[1] > 15
            or timeout[0] > timeout[1]
        ):
            raise ValueError("companion timeout is invalid")
        if not hasattr(signer, "issue"):
            raise ValueError("an actor assertion signer is required")
        self._base_url = base_url
        self._endpoint = (base_url or "") + endpoint_suffix
        self._signer = signer
        self.policy_helper = policy_helper
        self._transport = transport
        self.timeout = (float(timeout[0]), float(timeout[1]))
        self._request_lock = threading.Lock()
        self._request_id = 0

    def __repr__(self) -> str:
        return f"{type(self).__name__}(configured={bool(self._base_url or self._transport)}, signer=<redacted>)"

    @property
    def endpoint(self) -> str:
        return "<companion-endpoint>"

    @property
    def tool_names(self) -> tuple[str, ...]:
        return TOOL_INVENTORY

    @property
    def tool_inventory(self) -> tuple[str, ...]:
        return TOOL_INVENTORY

    def _next_request_id(self) -> int:
        with self._request_lock:
            self._request_id += 1
            return self._request_id

    def _policy(self, context: TrustedTelegramContext, tool: str) -> PolicyMembership:
        if self.policy_helper is None:
            raise CompanionAuthorizationError("current Telegram policy is unavailable")
        require_admin = tool in ADMIN_TOOL_SET
        helper = self.policy_helper
        authorize = getattr(helper, "authorize", None)
        if callable(authorize):
            try:
                result = authorize(
                    user_id=context.user_id,
                    chat_id=context.chat_id,
                    require_admin=require_admin,
                )
            except PolicyHelperDenied as exc:
                raise CompanionAuthorizationError(
                    "Telegram identity is not authorized"
                ) from exc
            except Exception as exc:
                raise CompanionUnavailable(
                    "current Telegram policy is unavailable"
                ) from exc
        else:
            membership = getattr(helper, "membership", None)
            if not callable(membership):
                raise CompanionAuthorizationError(
                    "current Telegram policy is unavailable"
                )
            try:
                result = membership(user_id=context.user_id, chat_id=context.chat_id)
            except Exception as exc:
                raise CompanionUnavailable(
                    "current Telegram policy is unavailable"
                ) from exc
            if not isinstance(result, PolicyMembership):
                raise CompanionAuthorizationError(
                    "current Telegram policy is unavailable"
                )
            if not result.is_authorized or (require_admin and not result.is_admin):
                raise CompanionAuthorizationError("Telegram identity is not authorized")
        if not isinstance(result, PolicyMembership):
            # Accept structural fakes while retaining a typed invariant for
            # production responses.
            try:
                result = PolicyMembership(
                    user_id=context.user_id,
                    chat_id=context.chat_id,
                    allowed=bool(result.allowed),
                    role=str(result.role),
                    fingerprint=str(result.fingerprint),
                    version=str(getattr(result, "version", "")),
                )
            except Exception as exc:
                raise CompanionAuthorizationError(
                    "current Telegram policy is unavailable"
                ) from exc
        if not result.is_authorized or (require_admin and not result.is_admin):
            raise CompanionAuthorizationError("Telegram identity is not authorized")
        return result

    def build_actor_assertion(
        self,
        *,
        context: TrustedTelegramContext | None = None,
        tool: str,
        arguments: Mapping[str, Any],
        role: str,
        allowlist_fingerprint: str,
    ) -> str:
        # Explicit context is accepted only as an identity-preserving alias for
        # tests/integrations.  It cannot bypass the contextvar boundary.
        active = require_trusted_context()
        if context is not None and context != active:
            raise TrustedContextError(
                "explicit actor context differs from native context"
            )
        try:
            return cast(
                str,
                self._signer.issue(
                    audience=COMPANION_AUDIENCE,
                    tool=tool,
                    arguments=dict(arguments),
                    user_id=active.user_id,
                    chat_id=active.chat_id,
                    chat_type=active.chat_type,
                    role=role,
                    update_id=active.update_id,
                    update_type=active.update_type,
                    message_id=active.message_id,
                    callback_query_id=active.callback_query_id,
                    allowlist_fingerprint=allowlist_fingerprint,
                ),
            )
        except Exception as exc:
            raise CompanionAuthorizationError(
                "actor assertion could not be created"
            ) from exc

    def _request(self, body: bytes, headers: Mapping[str, str]) -> object:
        return self._request_to(self._endpoint, body, headers)

    def _request_to(
        self, endpoint: str, body: bytes, headers: Mapping[str, str]
    ) -> object:
        if self._transport is None and not self._base_url:
            raise CompanionUnavailable("companion is not configured")
        target = (
            getattr(self._transport, "request", self._transport)
            if self._transport is not None
            else None
        )
        target_endpoint = (
            (self._base_url or "") + endpoint if endpoint.startswith("/") else endpoint
        )
        try:
            if callable(target):
                try:
                    return target(
                        "POST",
                        target_endpoint,
                        headers=dict(headers),
                        data=body,
                        timeout=self.timeout,
                    )
                except TypeError:
                    return target(
                        method="POST",
                        url=target_endpoint,
                        headers=dict(headers),
                        data=body,
                        timeout=self.timeout,
                    )
            import requests

            return requests.post(
                target_endpoint,
                headers=dict(headers),
                data=body,
                timeout=self.timeout,
            )
        except Exception as exc:
            raise CompanionUnavailable("companion request failed") from exc

    def call_tool(
        self, tool: str, arguments: Mapping[str, Any] | None = None
    ) -> CompanionResult:
        if not isinstance(tool, str) or tool not in TOOL_SET:
            raise CompanionToolDenied(
                "tool is not part of the frozen companion surface"
            )
        safe_arguments = _safe_args({} if arguments is None else arguments)
        context = require_trusted_context()
        membership = self._policy(context, tool)
        assertion = self.build_actor_assertion(
            context=context,
            tool=tool,
            arguments=safe_arguments,
            role=membership.role,
            allowlist_fingerprint=membership.fingerprint,
        )
        request_id = self._next_request_id()
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": tool, "arguments": safe_arguments},
        }
        body = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8", "strict"
        )
        if len(body) > MAX_REQUEST_BYTES:
            raise CompanionToolDenied("tool request exceeds the request bound")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            ACTOR_HEADER: assertion,
        }
        return _decode_result(tool, self._request(body, headers))

    async def call_tool_async(
        self, tool: str, arguments: Mapping[str, Any] | None = None
    ) -> CompanionResult:
        # A caller-provided async fake can expose request_async; otherwise the
        # bounded synchronous operation runs off the event loop.
        request_async = getattr(self._transport, "request_async", None)
        if callable(request_async):
            # Build/sign exactly once in the same path as sync call, while
            # swapping only the final transport operation.
            return await asyncio.to_thread(self.call_tool, tool, arguments)
        return await asyncio.to_thread(self.call_tool, tool, arguments)

    def callback(
        self, *, token: str, callback_query_id: str, chat_id: int, message_id: int
    ) -> CompanionResult:
        """Invoke the private confirmation callback route as a typed action.

        This method is used only by :class:`ConfirmationCallbackHandler`; it
        is intentionally not registered as a model-visible tool.
        """

        context = require_trusted_context()
        token = _validate_confirmation_token(token)
        if not isinstance(callback_query_id, str) or not callback_query_id.strip():
            raise CompanionProtocolError("confirmation callback query ID is invalid")
        chat_id = _validate_chat_id(chat_id)
        message_id = _validate_positive_id(message_id, name="message ID")
        if context.chat_id != chat_id or context.message_id != message_id:
            raise TrustedContextError(
                "confirmation provenance differs from native context"
            )
        membership = self._policy(context, "repair_blocked_imports")
        assertion = self.build_actor_assertion(
            context=context,
            tool="confirmation_callback",
            arguments={
                "token": token,
                "callback_query_id": callback_query_id,
                "chat_id": chat_id,
                "message_id": message_id,
            },
            role=membership.role,
            allowlist_fingerprint=membership.fingerprint,
        )
        body = json.dumps(
            {
                "token": token,
                "callback_query_id": callback_query_id,
                "chat_id": chat_id,
                "message_id": message_id,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8", "strict")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            ACTOR_HEADER: assertion,
        }

        return _decode_result(
            "confirmation_callback",
            self._request_to(PRIVATE_CONFIRM_CALLBACK_ENDPOINT, body, headers),
        )

    def bind_confirmation(
        self,
        *,
        token: str,
        preview: str,
        chat_id: int,
        message_id: int,
    ) -> CompanionResult:
        """Bind exact server preview bytes to one native Telegram message."""

        context = require_trusted_context()
        chat_id = _validate_chat_id(chat_id)
        message_id = _validate_positive_id(message_id, name="message ID")
        if context.chat_id != chat_id:
            raise TrustedContextError("confirmation chat differs from native context")
        envelope = ConfirmationEnvelope(token=token, preview=preview)
        membership = self._policy(context, "repair_blocked_imports")
        assertion = self.build_actor_assertion(
            context=context,
            tool="confirmation_bind",
            arguments={
                "token": envelope.token,
                "preview": envelope.preview,
                "chat_id": chat_id,
                "message_id": message_id,
            },
            role=membership.role,
            allowlist_fingerprint=membership.fingerprint,
        )
        body = json.dumps(
            {
                "token": envelope.token,
                "preview": envelope.preview,
                "chat_id": chat_id,
                "message_id": message_id,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8", "strict")
        if len(body) > MAX_REQUEST_BYTES:
            raise CompanionToolDenied("confirmation bind exceeds the request bound")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            ACTOR_HEADER: assertion,
        }
        return _decode_result(
            "confirmation_bind",
            self._request_to(PRIVATE_CONFIRM_BIND_ENDPOINT, body, headers),
        )

    def register_tools(self, ctx: object) -> tuple[str, ...]:
        """Register exactly the frozen wrapper inventory on a Hermes context."""

        register = getattr(ctx, "register_tool", None)
        if not callable(register):
            raise CompanionToolDenied(
                "Hermes plugin context has no typed tool registration"
            )
        # Keep this compatibility registration path on the same checked-in
        # schemas as the native plugin path.  A generic open object here would
        # silently widen the model surface whenever a fake/test context uses
        # ``CompanionClient.register_tools`` directly.
        try:
            from .plugin import load_frozen_tool_metadata, load_frozen_tool_schemas

            schemas = load_frozen_tool_schemas()
            metadata = load_frozen_tool_metadata()
        except Exception as exc:  # noqa: BLE001
            raise CompanionToolDenied(
                "frozen companion tool contract is unavailable"
            ) from exc
        if set(schemas) != set(TOOL_INVENTORY) or set(metadata) != set(TOOL_INVENTORY):
            raise CompanionToolDenied("frozen companion tool inventory is invalid")
        registered: list[str] = []
        for tool in TOOL_INVENTORY:

            async def handler(
                args: Mapping[str, Any], _tool: str = tool
            ) -> dict[str, Any]:
                return (await self.call_tool_async(_tool, args)).to_dict()

            register(
                name=tool,
                toolset=(
                    "media-policy-shared"
                    if tool in SHARED_TOOL_SET
                    else "media-policy-admin"
                ),
                schema=dict(schemas[tool]),
                handler=handler,
                is_async=True,
                description=str(metadata[tool]["description"]),
                emoji="🎬",
            )
            registered.append(tool)
        return tuple(registered)


def _load_signer(key: str | bytes):
    try:
        auth_module = importlib.import_module("media_companion.auth")
        signer_type = auth_module.ActorAssertionSigner
        return signer_type(key, kid="current")
    except (ImportError, AttributeError, TypeError, ValueError) as exc:
        raise CompanionUnavailable("actor assertion signer is unavailable") from exc


def _read_key_file(path: str | os.PathLike[str]) -> bytes:
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise CompanionUnavailable("actor signing key is unavailable") from exc
    if not data or len(data) > 16 * 1024:
        raise CompanionUnavailable("actor signing key is invalid")
    # Secret files conventionally end with one newline. The companion's
    # production verifier removes CR/LF framing through _read_secret_reference;
    # the Hermes signer must use the identical effective bytes or every signed
    # tool call is rejected even though both containers mount the same file.
    data = data.rstrip(b"\r\n")
    if not data:
        raise CompanionUnavailable("actor signing key is invalid")
    return data


def client_from_environment(
    *,
    transport: CompanionTransport | Callable[..., object] | None = None,
    policy_helper: PolicyHelperAPI | object | None = None,
) -> CompanionClient:
    """Build a client from deployment references without exposing credentials."""

    base_url = (
        os.getenv("CRBL_COMPANION_URL")
        or os.getenv("MEDIA_COMPANION_URL")
        or os.getenv("COMPANION_URL")
    )
    key_file = (
        os.getenv("CRBL_ACTOR_SIGNING_KEY_FILE")
        or os.getenv("ACTOR_SIGNING_KEY_FILE")
        or os.getenv("MEDIA_COMPANION_ACTOR_KEY_FILE")
    )
    key_inline = os.getenv("CRBL_ACTOR_SIGNING_KEY") or os.getenv("ACTOR_SIGNING_KEY")
    if key_file:
        key = _read_key_file(key_file)
    elif key_inline:
        key = key_inline.encode("utf-8", "strict")
    else:
        raise CompanionUnavailable("actor signing key is not configured")
    if policy_helper is None:
        helper_url = (
            os.getenv("CRBL_POLICY_HELPER_URL")
            or os.getenv("POLICY_HELPER_URL")
            or DEFAULT_POLICY_HELPER_URL
        )
        helper_key_file = os.getenv("CRBL_POLICY_HELPER_KEY_FILE") or os.getenv(
            "POLICY_HELPER_KEY_FILE"
        )
        policy_helper = PolicyHelperAPI(
            helper_url, key_file=helper_key_file, transport=transport
        )
    return CompanionClient(
        base_url,
        signer=_load_signer(key),
        policy_helper=policy_helper,
        transport=transport,
    )


# Friendly alias used by plugin factories.
from_environment = client_from_environment


__all__ = [
    "ACTOR_HEADER",
    "ADMIN_TOOLS",
    "DEFAULT_TIMEOUT",
    "PRIVATE_CONFIRM_BIND_ENDPOINT",
    "PRIVATE_CONFIRM_CALLBACK_ENDPOINT",
    "SHARED_TOOLS",
    "TOOL_INVENTORY",
    "TOOL_POLICY_AVAILABLE",
    "CompanionAuthorizationError",
    "CompanionClient",
    "CompanionError",
    "CompanionProtocolError",
    "CompanionResult",
    "CompanionToolDenied",
    "CompanionTransport",
    "CompanionUnavailable",
    "ConfirmationEnvelope",
    "DEFAULT_POLICY_HELPER_URL",
    "client_from_environment",
    "current_policy_membership",
    "from_environment",
    "policy_membership_scope",
]
