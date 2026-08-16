"""Trusted Telegram actor extraction at Hermes' native event boundary."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from media_gateway.types import Actor, Role


class TrustError(ValueError):
    pass


def _get(value: object, name: str) -> Any:
    return value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)


def actor_from_event(event: object) -> Actor:
    message = _get(event, "raw_message")
    source = _get(event, "source")
    if message is None or source is None:
        raise TrustError("native Telegram event is incomplete")
    sender = _get(message, "from_user") or _get(message, "sender_chat")
    chat = _get(message, "chat")
    if sender is None or chat is None:
        raise TrustError("native Telegram identity is unavailable")
    user_id = _get(sender, "id")
    chat_id = _get(chat, "id")
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise TrustError("native Telegram user ID is invalid")
    if isinstance(chat_id, bool) or not isinstance(chat_id, int) or chat_id == 0:
        raise TrustError("native Telegram chat ID is invalid")
    source_user = _get(source, "user_id")
    source_chat = _get(source, "chat_id")
    if source_user is not None and str(source_user) != str(user_id):
        raise TrustError("Hermes and Telegram user identities differ")
    if source_chat is not None and str(source_chat) != str(chat_id):
        raise TrustError("Hermes and Telegram chat identities differ")
    return Actor(
        user_id=user_id,
        chat_id=chat_id,
        username=_text(_get(sender, "username")),
        first_name=_text(_get(sender, "first_name")),
        last_name=_text(_get(sender, "last_name")),
    )


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:128] or None


_ACTOR: ContextVar[Actor | None] = ContextVar("crbl_media_actor", default=None)
_ROLE: ContextVar[Role | None] = ContextVar("crbl_media_role", default=None)
_SESSION_KEY: ContextVar[str | None] = ContextVar("crbl_media_session_key", default=None)
_TURN_TEXT: ContextVar[str] = ContextVar("crbl_media_turn_text", default="")
_RECOMMENDATION_TURN: ContextVar[bool] = ContextVar("crbl_media_recommendation_turn", default=False)


def session_key_from_event(
    event: object,
    actor: Actor,
    *,
    group_sessions_per_user: bool = True,
    thread_sessions_per_user: bool = False,
) -> str:
    """Build Hermes' canonical session key from the trusted native source."""

    source = _get(event, "source")
    if source is None:
        raise TrustError("native Telegram session source is unavailable")
    try:
        from gateway.session import build_session_key  # type: ignore[import-not-found]
    except ImportError:
        # Hermes is intentionally absent from the standalone unit-test image.
        return f"agent:main:telegram:dm:{actor.chat_id}"
    try:
        session_key = build_session_key(
            source,
            group_sessions_per_user=group_sessions_per_user,
            thread_sessions_per_user=thread_sessions_per_user,
        )
    except Exception as exc:
        raise TrustError("native Telegram session is invalid") from exc
    if not isinstance(session_key, str) or not session_key:
        raise TrustError("native Telegram session key is invalid")
    return session_key


@contextmanager
def actor_scope(
    actor: Actor,
    role: Role,
    session_key: str | None = None,
    *,
    turn_text: str = "",
    recommendation_turn: bool = False,
) -> Iterator[None]:
    actor_token = _ACTOR.set(actor)
    role_token = _ROLE.set(role)
    session_token = _SESSION_KEY.set(session_key or f"agent:main:telegram:dm:{actor.chat_id}")
    turn_text_token = _TURN_TEXT.set(turn_text)
    recommendation_token = _RECOMMENDATION_TURN.set(recommendation_turn)
    try:
        yield
    finally:
        _RECOMMENDATION_TURN.reset(recommendation_token)
        _TURN_TEXT.reset(turn_text_token)
        _SESSION_KEY.reset(session_token)
        _ROLE.reset(role_token)
        _ACTOR.reset(actor_token)


def require_actor() -> Actor:
    actor = _ACTOR.get()
    if actor is None:
        raise TrustError("no trusted Telegram actor is active")
    return actor


def require_session_key() -> str:
    session_key = _SESSION_KEY.get()
    if session_key is None:
        raise TrustError("no trusted Telegram session is active")
    return session_key


def current_turn_text() -> str:
    return _TURN_TEXT.get()


def is_recommendation_turn() -> bool:
    return _RECOMMENDATION_TURN.get()


def current_role() -> Role | None:
    return _ROLE.get()
