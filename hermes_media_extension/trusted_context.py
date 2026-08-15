"""Trusted Telegram provenance for the Hermes media-policy extension.

The model is never an identity source.  A :class:`TrustedTelegramContext` is
created only at the native Telegram adapter boundary, from a Hermes
``MessageEvent`` and its native Telegram update/message objects.  The context
is immutable and is carried through a :class:`contextvars.ContextVar` so
companion wrappers do not need (or accept) user supplied identity arguments.

This module intentionally does not import Hermes or python-telegram-bot at
module import time.  That keeps unit tests useful without a Hermes install and
also lets the deployment contract verify the real native adapter separately.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator, Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass


class TrustedContextError(ValueError):
    """Raised when a native event cannot provide complete Telegram provenance."""


_MISSING = object()


def _attr(value: object, name: str, default: object = _MISSING) -> object:
    """Read an attribute from an object or mapping without invoking methods."""

    if isinstance(value, Mapping):
        result = value.get(name, default)
    else:
        result = getattr(value, name, default)
    return result


def _required_int(value: object, name: str, *, positive: bool = False) -> int:
    if isinstance(value, bool):
        raise TrustedContextError(f"native Telegram {name} is invalid")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str):
        try:
            result = int(value, 10)
        except (TypeError, ValueError, OverflowError) as exc:
            raise TrustedContextError(f"native Telegram {name} is invalid") from exc
        # Native PTB values are integers.  This rejects floats and values such
        # as ``"01"`` rather than silently changing an identity binding.
        if value.strip() != str(result):
            raise TrustedContextError(f"native Telegram {name} is invalid")
    else:
        raise TrustedContextError(f"native Telegram {name} is invalid")
    if positive and result <= 0:
        raise TrustedContextError(f"native Telegram {name} is invalid")
    return result


def _optional_int(value: object, name: str, *, positive: bool = True) -> int | None:
    if value is _MISSING or value is None or value == "":
        return None
    return _required_int(value, name, positive=positive)


def _text(value: object, name: str, *, max_bytes: int = 256) -> str:
    if not isinstance(value, str):
        raise TrustedContextError(f"native Telegram {name} is invalid")
    value = value.strip()
    if not value or len(value.encode("utf-8", "strict")) > max_bytes:
        raise TrustedContextError(f"native Telegram {name} is invalid")
    return value


def _chat_type(value: object) -> str:
    raw = getattr(value, "value", value)
    if not isinstance(raw, str):
        raise TrustedContextError("native Telegram chat type is invalid")
    normalized = raw.rsplit(".", 1)[-1].strip().lower()
    aliases = {"private": "private", "dm": "private"}
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"private", "group", "supergroup", "channel"}:
        raise TrustedContextError("native Telegram chat type is invalid")
    return normalized


def _native_message(update: object, *, callback: bool = False) -> object:
    """Return the native message carried by an update-like object."""

    if callback:
        query = _attr(update, "callback_query")
        message = _attr(query, "message") if query is not _MISSING else _MISSING
        if message is not _MISSING and message is not None:
            return message
    message = _attr(update, "effective_message")
    if message is _MISSING or message is None:
        message = _attr(update, "message")
    if message is _MISSING or message is None:
        message = _attr(update, "channel_post")
    if message is _MISSING or message is None:
        raise TrustedContextError("native Telegram update has no message")
    return message


def _native_user(update: object, message: object, *, callback: bool = False) -> object:
    if callback:
        query = _attr(update, "callback_query")
        user = _attr(query, "from_user") if query is not _MISSING else _MISSING
        if user is not _MISSING and user is not None:
            return user
    user = _attr(message, "from_user")
    if user is _MISSING or user is None:
        # Channel posts have no from_user.  Hermes native Telegram admission
        # treats sender_chat as the actor, so preserve that binding rather than
        # inventing an identity.
        user = _attr(message, "sender_chat")
    if user is _MISSING or user is None:
        raise TrustedContextError("native Telegram update has no sender")
    return user


@dataclass(frozen=True, slots=True)
class TrustedTelegramContext:
    """Immutable provenance extracted from one admitted native update.

    ``user_id`` and ``chat_id`` are numeric Telegram IDs.  ``update_id`` is
    the native long-poll offset and is intentionally part of every actor
    assertion.  ``callback_query_id`` is populated only for callback updates;
    callback handlers can therefore bind a click to the exact Telegram query
    without placing an assertion in ``callback_data``.
    """

    user_id: int
    chat_id: int
    chat_type: str
    update_id: int
    update_type: str = "message"
    message_id: int | None = None
    callback_query_id: str | None = None
    thread_id: int | None = None

    def __post_init__(self) -> None:
        if self.user_id <= 0:
            raise TrustedContextError("trusted Telegram user ID is invalid")
        if self.chat_id == 0:
            raise TrustedContextError("trusted Telegram chat ID is invalid")
        if self.update_id < 0:
            raise TrustedContextError("trusted Telegram update ID is invalid")
        object.__setattr__(self, "chat_type", _chat_type(self.chat_type))
        if self.update_type not in {
            "message",
            "callback_query",
            "edited_message",
            "channel_post",
        }:
            raise TrustedContextError("trusted Telegram update type is invalid")
        if self.message_id is not None and self.message_id <= 0:
            raise TrustedContextError("trusted Telegram message ID is invalid")
        if self.thread_id is not None and self.thread_id <= 0:
            raise TrustedContextError("trusted Telegram thread ID is invalid")
        if self.callback_query_id is not None:
            _text(self.callback_query_id, "callback query ID", max_bytes=256)

    @property
    def is_private_chat(self) -> bool:
        return self.chat_type == "private"

    @classmethod
    def from_update(cls, update: object) -> TrustedTelegramContext:
        """Extract context from a native Telegram update-like object.

        The function accepts duck-typed fakes, but requires the same fields
        Hermes obtains from python-telegram-bot.  It never reads an arbitrary
        ``user_id``/``chat_id`` field from model arguments.
        """

        if update is None:
            raise TrustedContextError("a native Telegram update is required")
        query = _attr(update, "callback_query")
        is_callback = query is not _MISSING and query is not None
        message = _native_message(update, callback=is_callback)
        user = _native_user(update, message, callback=is_callback)
        chat = _attr(message, "chat")
        if chat is _MISSING or chat is None:
            raise TrustedContextError("native Telegram message has no chat")
        update_id = _attr(update, "update_id")
        if update_id is _MISSING:
            raise TrustedContextError("native Telegram update has no update ID")
        user_id = _attr(user, "id")
        chat_id = _attr(chat, "id")
        message_id = _attr(message, "message_id")
        thread_id = _attr(message, "message_thread_id")
        update_type = "callback_query" if is_callback else "message"
        channel_post = _attr(update, "channel_post")
        if channel_post is not _MISSING and channel_post is not None:
            update_type = "channel_post"
        edited_message = _attr(update, "edited_message")
        if edited_message is not _MISSING and edited_message is not None:
            update_type = "edited_message"
        callback_query_id = _attr(query, "id") if is_callback else _MISSING
        return cls(
            user_id=_required_int(user_id, "user ID", positive=True),
            chat_id=_required_int(chat_id, "chat ID"),
            chat_type=_chat_type(_attr(chat, "type")),
            update_id=_required_int(update_id, "update ID", positive=False),
            update_type=update_type,
            message_id=_optional_int(message_id, "message ID"),
            callback_query_id=(
                _text(callback_query_id, "callback query ID")
                if callback_query_id is not _MISSING and callback_query_id is not None
                else None
            ),
            thread_id=_optional_int(thread_id, "message thread ID"),
        )

    @classmethod
    def from_event(cls, event: object) -> TrustedTelegramContext:
        """Extract context from a native Hermes ``MessageEvent``.

        ``raw_message`` is mandatory.  A model-created/synthetic event that
        only contains the normalized ``source`` fields is intentionally not a
        trust source.
        """

        raw_message = _attr(event, "raw_message")
        if raw_message is _MISSING or raw_message is None:
            raise TrustedContextError("Hermes event is missing native raw_message")
        source = _attr(event, "source")
        if source is _MISSING or source is None:
            raise TrustedContextError("Hermes event is missing native source")
        update_id = _attr(event, "platform_update_id")
        if update_id is _MISSING or update_id is None:
            raise TrustedContextError("Hermes event is missing native update ID")
        # Build a tiny update facade around the native raw message.  The event
        # source is checked against it below; source values are not trusted by
        # themselves.
        update = _NativeEventFacade(
            update_id=update_id,
            message=raw_message,
            callback_query=None,
        )
        context = cls.from_update(update)
        source_user = _attr(source, "user_id")
        source_chat = _attr(source, "chat_id")
        if (
            source_user is not _MISSING
            and source_user is not None
            and _required_int(source_user, "event user ID", positive=True)
            != context.user_id
        ):
            raise TrustedContextError("Hermes event/source identity mismatch")
        if (
            source_chat is not _MISSING
            and source_chat is not None
            and _required_int(source_chat, "event chat ID") != context.chat_id
        ):
            raise TrustedContextError("Hermes event/source chat mismatch")
        event_message_id = _attr(event, "message_id")
        if (
            event_message_id is not _MISSING
            and event_message_id is not None
            and _optional_int(event_message_id, "event message ID")
            != context.message_id
        ):
            raise TrustedContextError("Hermes event/native message mismatch")
        return context

    # Compatibility spellings make the boundary easy to discover without
    # creating alternate, less strict constructors.
    from_native_update = from_update
    from_native_event = from_event


@dataclass(frozen=True, slots=True)
class _NativeEventFacade:
    update_id: object
    message: object
    callback_query: object | None


trusted_context_var: ContextVar[TrustedTelegramContext | None] = ContextVar(
    "hermes_media_trusted_telegram_context", default=None
)
# Explicit alias used by integrations that prefer a longer name.
TRUSTED_CONTEXT: ContextVar[TrustedTelegramContext | None] = trusted_context_var


def current_trusted_context() -> TrustedTelegramContext | None:
    return trusted_context_var.get()


def require_trusted_context() -> TrustedTelegramContext:
    context = trusted_context_var.get()
    if context is None:
        raise TrustedContextError("no native Telegram trusted context is active")
    return context


@contextlib.contextmanager
def trusted_context_scope(
    context: TrustedTelegramContext,
) -> Iterator[TrustedTelegramContext]:
    """Install one immutable context for the duration of a native dispatch."""

    if not isinstance(context, TrustedTelegramContext):
        raise TypeError("context must be TrustedTelegramContext")
    token: Token[TrustedTelegramContext | None] = trusted_context_var.set(context)
    try:
        yield context
    finally:
        trusted_context_var.reset(token)


# Short aliases retained for callers/tests that describe the operation as
# binding rather than scoping.
bind_trusted_context = trusted_context_scope
get_trusted_context = current_trusted_context


def trusted_context_from_event(event: object) -> TrustedTelegramContext:
    return TrustedTelegramContext.from_event(event)


def trusted_context_from_update(update: object) -> TrustedTelegramContext:
    return TrustedTelegramContext.from_update(update)


__all__ = [
    "TRUSTED_CONTEXT",
    "TrustedContextError",
    "TrustedTelegramContext",
    "bind_trusted_context",
    "current_trusted_context",
    "get_trusted_context",
    "require_trusted_context",
    "trusted_context_from_event",
    "trusted_context_from_update",
    "trusted_context_scope",
    "trusted_context_var",
]
