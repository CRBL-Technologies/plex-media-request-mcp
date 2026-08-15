"""Native Telegram handling for CRBL confirmation callbacks.

Hermes v2026.8.3 installs one catch-all native callback handler.  The media
policy adapter intercepts only the ``crblc:`` prefix before delegating other
native callbacks to Hermes.  Callback data contains an opaque 256-bit token
only; actor assertions are minted internally from the callback update and are
never placed in Telegram data or model-visible arguments.
"""

from __future__ import annotations

import importlib
import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast

from .trusted_context import (
    TrustedContextError,
    TrustedTelegramContext,
    trusted_context_from_update,
    trusted_context_scope,
)

try:
    _auth_module: Any = importlib.import_module("media_companion.auth")
except ImportError:  # pragma: no cover - Hermes-only discovery.
    _auth_module = None

CONFIRMATION_CALLBACK_PREFIX: str = str(
    getattr(_auth_module, "CONFIRMATION_CALLBACK_PREFIX", "crblc:")
)


def _unavailable_callback_data(token: object) -> str:
    raise ValueError("media companion authentication is unavailable")


def _unavailable_parse_callback_data(data: object) -> str:
    raise ValueError("media companion authentication is unavailable")


def _unavailable_hash_token(token: str) -> str:
    raise ValueError("media companion authentication is unavailable")


confirmation_callback_data: Callable[[object], str] = cast(
    Callable[[object], str],
    getattr(_auth_module, "confirmation_callback_data", _unavailable_callback_data),
)
parse_confirmation_callback_data: Callable[[object], str] = cast(
    Callable[[object], str],
    getattr(
        _auth_module,
        "parse_confirmation_callback_data",
        _unavailable_parse_callback_data,
    ),
)
hash_confirmation_token: Callable[[str], str] = cast(
    Callable[[str], str],
    getattr(_auth_module, "hash_confirmation_token", _unavailable_hash_token),
)


class CallbackHandlingError(RuntimeError):
    """Base class for callback boundary failures."""


class CallbackRejected(CallbackHandlingError):
    """A callback was malformed, stale, or not authorized."""


class CallbackClient(Protocol):
    def callback(
        self, *, token: str, callback_query_id: str, chat_id: int, message_id: int
    ) -> object: ...


def _get(value: object, name: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


async def _maybe_await(value: object) -> object:
    if inspect.isawaitable(value):
        return await value  # type: ignore[misc]
    return value


def _callback_query(update: object) -> object | None:
    query = _get(update, "callback_query")
    return None if query is None else query


def _callback_message(query: object) -> object | None:
    message = _get(query, "message")
    return None if message is None else message


def _callback_text(result: object) -> str:
    text = getattr(result, "text", None)
    if isinstance(text, str) and text:
        # The companion controls response content; keep the Telegram UI
        # neutral even if an implementation returns a long provider message.
        return "Completed." if len(text.encode("utf-8", "strict")) > 128 else text
    return "Completed."


@dataclass(frozen=True, slots=True)
class CallbackOutcome:
    handled: bool
    accepted: bool
    token_hash: str | None = None
    message: str = ""


class ConfirmationCallbackHandler:
    """Consume exactly one CRBL callback prefix using native callback context."""

    prefix = CONFIRMATION_CALLBACK_PREFIX
    callback_prefix = CONFIRMATION_CALLBACK_PREFIX

    def __init__(
        self,
        client: CallbackClient | object,
        *,
        executor: Callable[..., object] | None = None,
    ) -> None:
        self.client = client
        self.executor = executor

    @classmethod
    def owns(cls, data: object) -> bool:
        return isinstance(data, str) and data.startswith(cls.prefix)

    handles = owns
    is_crbl_callback = owns

    @staticmethod
    def callback_data(token: object) -> str:
        try:
            return confirmation_callback_data(token)  # type: ignore[arg-type]
        except Exception as exc:
            raise CallbackRejected("invalid confirmation token") from exc

    make_callback_data = callback_data

    async def _answer(self, query: object, text: str) -> None:
        answer = _get(query, "answer")
        if callable(answer):
            try:
                await _maybe_await(answer(text=text))
            except TypeError:
                await _maybe_await(answer(text))
            except Exception:  # noqa: BLE001
                # Telegram UI acknowledgement is best-effort; the durable
                # companion decision remains authoritative.
                return

    async def _clear_buttons(self, query: object) -> None:
        edit = _get(query, "edit_message_reply_markup")
        if callable(edit):
            try:
                await _maybe_await(edit(reply_markup=None))
                return
            except Exception:  # noqa: BLE001,S110
                pass
        edit_text = _get(query, "edit_message_text")
        message = _callback_message(query)
        text = _get(message, "text", "") if message is not None else ""
        if callable(edit_text):
            try:
                await _maybe_await(
                    edit_text(text=str(text or "Completed."), reply_markup=None)
                )
            except Exception:  # noqa: BLE001,S110
                pass

    async def _invoke(
        self, token: str, context: TrustedTelegramContext, query: object
    ) -> object:
        callback_query_id = _get(query, "id")
        if not isinstance(callback_query_id, str) or not callback_query_id.strip():
            raise CallbackRejected("callback query ID is missing")
        message = _callback_message(query)
        if message is None:
            raise CallbackRejected("callback message is missing")
        chat = _get(message, "chat")
        message_id = _get(message, "message_id")
        chat_id = _get(chat, "id")
        if (
            not isinstance(message_id, int)
            or isinstance(message_id, bool)
            or message_id <= 0
        ):
            raise CallbackRejected("callback message ID is invalid")
        if not isinstance(chat_id, int) or isinstance(chat_id, bool) or chat_id == 0:
            raise CallbackRejected("callback chat ID is invalid")
        if context.chat_id != chat_id or context.message_id != message_id:
            raise CallbackRejected("callback provenance does not match native message")
        if self.executor is not None:
            result = self.executor(
                token=token,
                callback_query_id=callback_query_id,
                chat_id=chat_id,
                message_id=message_id,
                context=context,
            )
        else:
            callback = getattr(self.client, "callback", None)
            if not callable(callback):
                raise CallbackRejected("confirmation callback client is unavailable")
            result = callback(
                token=token,
                callback_query_id=callback_query_id,
                chat_id=chat_id,
                message_id=message_id,
            )
        return await _maybe_await(result)

    async def handle_update(
        self, update: object, _context: object | None = None
    ) -> CallbackOutcome:
        query = _callback_query(update)
        if query is None:
            return CallbackOutcome(False, False, message="not a callback update")
        data = _get(query, "data")
        if not self.owns(data):
            return CallbackOutcome(False, False, message="not a CRBL callback")
        try:
            token = parse_confirmation_callback_data(data)  # type: ignore[arg-type]
            native = trusted_context_from_update(update)
            with trusted_context_scope(native):
                result = await self._invoke(token, native, query)
            await self._answer(query, _callback_text(result))
            await self._clear_buttons(query)
            # Store only a one-way token digest when an outcome is returned.
            return CallbackOutcome(
                True,
                True,
                token_hash=hash_confirmation_token(token),
                message="accepted",
            )
        except (
            TrustedContextError,
            CallbackHandlingError,
            ValueError,
            TypeError,
        ):
            await self._answer(query, "This confirmation is no longer valid.")
            await self._clear_buttons(query)
            return CallbackOutcome(True, False, message="rejected")
        except Exception:  # noqa: BLE001
            # Do not leak token/provider/error details to Telegram or logs.
            await self._answer(query, "Temporarily unavailable.")
            return CallbackOutcome(True, False, message="unavailable")

    # PTB's callback handler is a two-argument coroutine.  Keep this spelling
    # so the adapter can bind it directly in tests or future native hooks.
    async def __call__(
        self, update: object, context: object | None = None
    ) -> CallbackOutcome:
        return await self.handle_update(update, context)

    async def handle(
        self, update: object, context: object | None = None
    ) -> CallbackOutcome:
        return await self.handle_update(update, context)


__all__ = [
    "CONFIRMATION_CALLBACK_PREFIX",
    "CallbackHandlingError",
    "CallbackOutcome",
    "CallbackRejected",
    "ConfirmationCallbackHandler",
]
