"""Durable gateway state with a deliberately small SQLite schema."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .types import Actor, Role

SCHEMA_VERSION = 1
TABLE_COLUMNS = {
    "users": {
        "user_id",
        "chat_id",
        "username",
        "first_name",
        "last_name",
        "first_seen",
        "last_seen",
        "last_blocked",
    },
    "activity": {"id", "occurred_at", "kind", "user_id", "label"},
    "requests": {
        "id",
        "media_type",
        "external_id",
        "seasons",
        "title",
        "year",
        "user_id",
        "chat_id",
        "state",
        "created_at",
        "fulfilled_at",
    },
    "media_events": {
        "event_key",
        "media_type",
        "external_id",
        "rating_key",
        "title",
        "show_title",
        "season_number",
        "episode_number",
        "parent_rating_key",
        "plex_url",
        "observed_at",
        "notified_at",
    },
    "deliveries": {"event_key", "chat_id", "delivered_at"},
}


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _migrate(self) -> None:
        with self._db() as db:
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
            if version not in {0, SCHEMA_VERSION}:
                raise RuntimeError(f"unsupported gateway database version: {version}")
            existing = {
                str(row["name"])
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            if version == 0 and existing & TABLE_COLUMNS.keys():
                raise RuntimeError("gateway database has an unversioned incompatible schema")
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    first_seen INTEGER NOT NULL,
                    last_seen INTEGER NOT NULL,
                    last_blocked INTEGER
                );
                CREATE TABLE IF NOT EXISTS activity (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred_at INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    user_id INTEGER,
                    label TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS activity_recent
                    ON activity(occurred_at DESC);
                CREATE TABLE IF NOT EXISTS requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    media_type TEXT NOT NULL CHECK(media_type IN ('movie','series')),
                    external_id INTEGER NOT NULL,
                    seasons TEXT NOT NULL DEFAULT '[]',
                    title TEXT NOT NULL,
                    year INTEGER,
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'requested',
                    created_at INTEGER NOT NULL,
                    fulfilled_at INTEGER,
                    UNIQUE(media_type, external_id, seasons, user_id)
                );
                CREATE INDEX IF NOT EXISTS requests_external
                    ON requests(media_type, external_id, state);
                CREATE TABLE IF NOT EXISTS media_events (
                    event_key TEXT PRIMARY KEY,
                    media_type TEXT NOT NULL,
                    external_id INTEGER,
                    rating_key TEXT NOT NULL,
                    title TEXT NOT NULL,
                    show_title TEXT,
                    season_number INTEGER,
                    episode_number INTEGER,
                    parent_rating_key TEXT,
                    plex_url TEXT NOT NULL,
                    observed_at INTEGER NOT NULL,
                    notified_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    event_key TEXT NOT NULL,
                    chat_id INTEGER NOT NULL,
                    delivered_at INTEGER NOT NULL,
                    PRIMARY KEY(event_key, chat_id)
                );
                """
            )
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            for table, expected in TABLE_COLUMNS.items():
                columns = {
                    str(row["name"])
                    for row in db.execute(f'PRAGMA table_info("{table}")').fetchall()
                }
                if columns != expected:
                    raise RuntimeError(f"gateway database table is incompatible: {table}")

    def prune(self, now: int | None = None) -> None:
        """Remove terminal operational data after the agreed 60-day window."""

        cutoff = (now or int(time.time())) - 60 * 24 * 60 * 60
        with self._db() as db:
            db.execute("DELETE FROM activity WHERE occurred_at < ?", (cutoff,))
            old_events = db.execute(
                "SELECT event_key FROM media_events WHERE observed_at < ?", (cutoff,)
            ).fetchall()
            keys = [str(row["event_key"]) for row in old_events]
            if keys:
                parameters = [(key,) for key in keys]
                db.executemany("DELETE FROM deliveries WHERE event_key=?", parameters)
                db.executemany("DELETE FROM media_events WHERE event_key=?", parameters)
            db.execute(
                "DELETE FROM requests WHERE state='available' AND fulfilled_at < ?", (cutoff,)
            )
            db.execute(
                """DELETE FROM users WHERE last_seen < ? AND user_id NOT IN
                (SELECT user_id FROM requests WHERE state='requested')""",
                (cutoff,),
            )

    def observe_actor(
        self, actor: Actor, *, blocked: bool = False, record_activity: bool = True
    ) -> None:
        now = int(time.time())
        with self._db() as db:
            db.execute(
                """
                INSERT INTO users(
                    user_id, chat_id, username, first_name, last_name,
                    first_seen, last_seen, last_blocked
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    chat_id=excluded.chat_id,
                    username=COALESCE(excluded.username, users.username),
                    first_name=COALESCE(excluded.first_name, users.first_name),
                    last_name=COALESCE(excluded.last_name, users.last_name),
                    last_seen=excluded.last_seen,
                    last_blocked=COALESCE(excluded.last_blocked, users.last_blocked)
                """,
                (
                    actor.user_id,
                    actor.chat_id,
                    actor.username,
                    actor.first_name,
                    actor.last_name,
                    now,
                    now,
                    now if blocked else None,
                ),
            )
            if record_activity:
                db.execute(
                    """INSERT INTO activity(occurred_at, kind, user_id, label)
                    VALUES (?, ?, ?, ?)""",
                    (
                        now,
                        "blocked" if blocked else "seen",
                        actor.user_id,
                        "Blocked message" if blocked else "Active user",
                    ),
                )

    def record_activity(self, kind: str, label: str, user_id: int | None = None) -> None:
        if len(kind) > 32 or len(label) > 200:
            raise ValueError("activity value is too long")
        with self._db() as db:
            db.execute(
                "INSERT INTO activity(occurred_at, kind, user_id, label) VALUES (?, ?, ?, ?)",
                (int(time.time()), kind, user_id, label),
            )

    def users(self, roles: dict[int, Role]) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM users ORDER BY last_seen DESC").fetchall()
        known = {int(row["user_id"]) for row in rows}
        result = [self._user_row(row, roles.get(int(row["user_id"]), Role.BLOCKED)) for row in rows]
        for user_id, role in sorted(roles.items()):
            if user_id not in known:
                result.append(
                    {
                        "user_id": user_id,
                        "chat_id": None,
                        "username": None,
                        "name": None,
                        "role": role.value,
                        "first_seen": None,
                        "last_seen": None,
                        "last_blocked": None,
                    }
                )
        return result

    @staticmethod
    def _user_row(row: sqlite3.Row, role: Role) -> dict[str, Any]:
        name = " ".join(part for part in (row["first_name"], row["last_name"]) if part)
        return {
            "user_id": int(row["user_id"]),
            "chat_id": int(row["chat_id"]),
            "username": row["username"],
            "name": name or None,
            "role": role.value,
            "first_seen": int(row["first_seen"]),
            "last_seen": int(row["last_seen"]),
            "last_blocked": int(row["last_blocked"]) if row["last_blocked"] else None,
        }

    def recent_activity(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute(
                "SELECT occurred_at, kind, user_id, label FROM activity ORDER BY id DESC LIMIT ?",
                (min(max(limit, 1), 200),),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_request(
        self,
        *,
        media_type: str,
        external_id: int,
        seasons: tuple[int, ...],
        title: str,
        year: int | None,
        actor: Actor,
    ) -> int:
        encoded = json.dumps(sorted(set(seasons)), separators=(",", ":"))
        now = int(time.time())
        with self._db() as db:
            db.execute(
                """
                INSERT INTO requests(
                    media_type, external_id, seasons, title, year,
                    user_id, chat_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(media_type, external_id, seasons, user_id) DO UPDATE SET
                    chat_id=excluded.chat_id,
                    title=excluded.title,
                    year=excluded.year,
                    state='requested',
                    created_at=excluded.created_at,
                    fulfilled_at=NULL
                """,
                (media_type, external_id, encoded, title, year, actor.user_id, actor.chat_id, now),
            )
            row = db.execute(
                """SELECT id FROM requests
                WHERE media_type=? AND external_id=? AND seasons=? AND user_id=?""",
                (media_type, external_id, encoded, actor.user_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("request row was not persisted")
            request_id = int(row["id"])
            db.execute(
                "INSERT INTO activity(occurred_at, kind, user_id, label) VALUES (?, ?, ?, ?)",
                (now, "request", actor.user_id, f"Requested {title}"),
            )
            return request_id

    def requests_for(self, user_id: int, *, all_users: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM requests"
        parameters: tuple[object, ...] = ()
        if not all_users:
            query += " WHERE user_id=?"
            parameters = (user_id,)
        query += " ORDER BY id DESC LIMIT 100"
        with self._db() as db:
            rows = db.execute(query, parameters).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            result.append(
                {
                    "id": int(row["id"]),
                    "media_type": row["media_type"],
                    "external_id": int(row["external_id"]),
                    "seasons": json.loads(row["seasons"]),
                    "title": row["title"],
                    "year": row["year"],
                    "user_id": int(row["user_id"]),
                    "state": row["state"],
                    "created_at": int(row["created_at"]),
                    "fulfilled_at": row["fulfilled_at"],
                }
            )
        return result

    def add_media_event(
        self,
        *,
        event_key: str,
        media_type: str,
        external_id: int | None,
        rating_key: str,
        title: str,
        show_title: str | None,
        season_number: int | None,
        episode_number: int | None,
        plex_url: str,
        parent_rating_key: str | None = None,
        observed_at: int | None = None,
    ) -> bool:
        now = observed_at or int(time.time())
        with self._db() as db:
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO media_events(
                    event_key, media_type, external_id, rating_key, title,
                    show_title, season_number, episode_number, parent_rating_key,
                    plex_url, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_key,
                    media_type,
                    external_id,
                    rating_key,
                    title[:300],
                    show_title[:300] if show_title else None,
                    season_number,
                    episode_number,
                    parent_rating_key,
                    plex_url,
                    now,
                ),
            )
            if cursor.rowcount:
                label = f"Plex added {show_title or title}"
                db.execute(
                    """INSERT INTO activity(occurred_at, kind, user_id, label)
                    VALUES (?, ?, NULL, ?)""",
                    (now, "available", label[:200]),
                )
            return cursor.rowcount == 1

    def set_media_external_id(self, event_key: str, external_id: int) -> None:
        with self._db() as db:
            db.execute(
                "UPDATE media_events SET external_id=? WHERE event_key=?",
                (external_id, event_key),
            )

    def pending_media_events(self, before: int, limit: int = 200) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute(
                """SELECT * FROM media_events
                WHERE notified_at IS NULL AND observed_at <= ?
                ORDER BY observed_at, event_key LIMIT ?""",
                (before, min(max(limit, 1), 500)),
            ).fetchall()
        return [dict(row) for row in rows]

    def request_chats(
        self, *, media_type: str, external_id: int | None, season_number: int | None
    ) -> set[int]:
        if external_id is None:
            return set()
        with self._db() as db:
            rows = db.execute(
                """SELECT chat_id, seasons FROM requests
                WHERE media_type=? AND external_id=? AND state='requested'""",
                (media_type, external_id),
            ).fetchall()
        chats: set[int] = set()
        for row in rows:
            seasons = json.loads(row["seasons"])
            if media_type == "movie" or season_number is None or season_number in seasons:
                chats.add(int(row["chat_id"]))
        return chats

    def mark_movie_available(self, external_id: int) -> None:
        now = int(time.time())
        with self._db() as db:
            db.execute(
                """UPDATE requests SET state='available', fulfilled_at=?
                WHERE media_type='movie' AND external_id=? AND state='requested'""",
                (now, external_id),
            )

    def delivered(self, event_keys: Iterable[str], chat_id: int) -> bool:
        keys = tuple(event_keys)
        if not keys:
            return True
        with self._db() as db:
            return all(
                db.execute(
                    "SELECT 1 FROM deliveries WHERE chat_id=? AND event_key=?",
                    (chat_id, key),
                ).fetchone()
                is not None
                for key in keys
            )

    def mark_delivered(self, event_keys: Iterable[str], chat_id: int) -> None:
        now = int(time.time())
        keys = tuple(event_keys)
        with self._db() as db:
            db.executemany(
                """INSERT OR IGNORE INTO deliveries(event_key, chat_id, delivered_at)
                VALUES (?, ?, ?)""",
                ((key, chat_id, now) for key in keys),
            )

    def mark_events_notified(self, event_keys: Iterable[str]) -> None:
        keys = tuple(event_keys)
        if not keys:
            return
        now = int(time.time())
        with self._db() as db:
            db.executemany(
                "UPDATE media_events SET notified_at=? WHERE event_key=?",
                ((now, key) for key in keys),
            )

    def recent_requests(self, limit: int = 25) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM requests ORDER BY id DESC LIMIT ?",
                (min(max(limit, 1), 100),),
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "media_type": row["media_type"],
                "external_id": int(row["external_id"]),
                "seasons": json.loads(row["seasons"]),
                "title": row["title"],
                "year": row["year"],
                "user_id": int(row["user_id"]),
                "state": row["state"],
                "created_at": int(row["created_at"]),
            }
            for row in rows
        ]
