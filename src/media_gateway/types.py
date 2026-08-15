"""Small shared data types."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Role(StrEnum):
    BLOCKED = "blocked"
    USER = "user"
    ADMIN = "admin"


@dataclass(frozen=True, slots=True)
class Actor:
    user_id: int
    chat_id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None

    @classmethod
    def from_json(cls, value: object) -> Actor:
        if not isinstance(value, dict):
            raise ValueError("actor must be an object")
        allowed = {"user_id", "chat_id", "username", "first_name", "last_name"}
        if set(value) - allowed:
            raise ValueError("actor contains unknown fields")
        user_id = value.get("user_id")
        chat_id = value.get("chat_id")
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            raise ValueError("actor.user_id must be a positive integer")
        if isinstance(chat_id, bool) or not isinstance(chat_id, int) or chat_id == 0:
            raise ValueError("actor.chat_id must be a non-zero integer")
        names: dict[str, str | None] = {}
        for key in ("username", "first_name", "last_name"):
            item = value.get(key)
            if item is not None and (not isinstance(item, str) or len(item) > 128):
                raise ValueError(f"actor.{key} must be a short string")
            names[key] = item or None
        return cls(user_id=user_id, chat_id=chat_id, **names)
