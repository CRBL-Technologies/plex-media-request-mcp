"""Short conversations on disk, so a restart does not interrupt them."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

Key = tuple[int, int]


class ConversationStore:
    """Holds only what the bot already keeps in memory, and drops it when it expires."""

    def __init__(self, path: Path, *, idle_seconds: float):
        self.idle_seconds = idle_seconds
        os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
        os.chmod(path, 0o600)
        self._db = sqlite3.connect(path)
        # Without this, deleted chats stay readable in the file's free pages.
        self._db.execute("PRAGMA secure_delete = ON")
        with self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS conversations ("
                "chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, "
                "messages TEXT NOT NULL, used_at REAL NOT NULL, "
                "PRIMARY KEY (chat_id, user_id))"
            )

    def load(self, key: Key, now: float) -> tuple[list[dict[str, str]], float]:
        row = self._db.execute(
            "SELECT messages, used_at FROM conversations WHERE chat_id = ? AND user_id = ?", key
        ).fetchone()
        if row is None or now - row[1] > self.idle_seconds:
            return [], 0.0
        return json.loads(row[0]), row[1]

    def save(self, key: Key, messages: list[dict[str, str]], now: float) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO conversations VALUES (?, ?, ?, ?)",
                (*key, json.dumps(messages, ensure_ascii=False), now),
            )
            self._db.execute(
                "DELETE FROM conversations WHERE used_at < ?", (now - self.idle_seconds,)
            )

    def clear(self, key: Key) -> None:
        with self._db:
            self._db.execute("DELETE FROM conversations WHERE chat_id = ? AND user_id = ?", key)
