"""Durable gateway state with a deliberately small SQLite schema."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .migrations import migrate
from .types import Actor, Page, Role


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
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _migrate(self) -> None:
        with self._db() as db:
            migrate(db)

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
                (SELECT user_id FROM requests WHERE state IN ('pending','requested','unknown'))""",
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
            if record_activity and blocked:
                db.execute(
                    """INSERT INTO activity(occurred_at, kind, user_id, label)
                    VALUES (?, ?, ?, ?)""",
                    (
                        now,
                        "blocked",
                        actor.user_id,
                        "Blocked message",
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

    @staticmethod
    def _page(number: int, page_size: int, total: int) -> tuple[int, int, int]:
        size = min(max(page_size, 1), 100)
        pages = max(1, (total + size - 1) // size)
        current = min(max(number, 1), pages)
        return current, pages, (current - 1) * size

    def activity_page(self, number: int = 1, page_size: int = 25) -> Page:
        with self._db() as db:
            total = int(
                db.execute("SELECT count(*) FROM activity WHERE kind != 'seen'").fetchone()[0]
            )
            current, pages, offset = self._page(number, page_size, total)
            rows = db.execute(
                """SELECT occurred_at, kind, user_id, label FROM activity
                WHERE kind != 'seen' ORDER BY occurred_at DESC, id DESC LIMIT ? OFFSET ?""",
                (min(max(page_size, 1), 100), offset),
            ).fetchall()
        return Page([dict(row) for row in rows], current, pages, total)

    def recent_activity(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.activity_page(1, limit).items

    def begin_request(
        self,
        *,
        media_type: str,
        external_id: int,
        seasons: tuple[int, ...],
        title: str,
        year: int | None,
        actor: Actor,
        options: dict[str, Any] | None = None,
    ) -> int:
        encoded = json.dumps(sorted(set(seasons)), separators=(",", ":"))
        encoded_options = json.dumps(options or {}, sort_keys=True, separators=(",", ":"))
        now = int(time.time())
        with self._db() as db:
            db.execute(
                """
                INSERT INTO requests(
                    media_type, external_id, seasons, title, year,
                    user_id, options, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                ON CONFLICT(media_type, external_id, seasons, user_id) DO UPDATE SET
                    title=excluded.title,
                    year=excluded.year,
                    options=excluded.options,
                    state='pending',
                    provider_status=NULL,
                    updated_at=excluded.updated_at,
                    fulfilled_at=NULL
                """,
                (
                    media_type,
                    external_id,
                    encoded,
                    title,
                    year,
                    actor.user_id,
                    encoded_options,
                    now,
                    now,
                ),
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
                """INSERT OR IGNORE INTO request_destinations(request_id, chat_id, created_at)
                VALUES (?, ?, ?)""",
                (request_id, actor.chat_id, now),
            )
            return request_id

    def complete_request(
        self, request_id: int, provider_status: str, *, record_activity: bool = True
    ) -> None:
        if not provider_status or len(provider_status) > 64:
            raise ValueError("provider status is invalid")
        now = int(time.time())
        state = "available" if provider_status == "available" else "requested"
        with self._db() as db:
            row = db.execute(
                "SELECT title, user_id FROM requests WHERE id=?", (request_id,)
            ).fetchone()
            if row is None:
                raise RuntimeError("request intent is missing")
            db.execute(
                """UPDATE requests SET state=?, provider_status=?, updated_at=?, fulfilled_at=?
                WHERE id=?""",
                (
                    state,
                    provider_status,
                    now,
                    now if state == "available" else None,
                    request_id,
                ),
            )
            if record_activity:
                db.execute(
                    """INSERT INTO activity(occurred_at, kind, user_id, label)
                    VALUES (?, 'request', ?, ?)""",
                    (now, int(row["user_id"]), f"Requested {row['title']}"),
                )

    def mark_request_unknown(self, request_id: int) -> None:
        with self._db() as db:
            cursor = db.execute(
                """UPDATE requests SET state='unknown', provider_status=NULL, updated_at=?
                WHERE id=?""",
                (int(time.time()), request_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("request intent is missing")

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
        """Compatibility helper for tests and trusted one-shot data setup."""

        request_id = self.begin_request(
            media_type=media_type,
            external_id=external_id,
            seasons=seasons,
            title=title,
            year=year,
            actor=actor,
        )
        self.complete_request(request_id, "requested")
        return request_id

    def pending_request_intents(
        self, limit: int = 100, *, updated_before: int | None = None
    ) -> list[dict[str, Any]]:
        cutoff = updated_before if updated_before is not None else 2**63 - 1
        with self._db() as db:
            rows = db.execute(
                """SELECT * FROM requests WHERE state IN ('pending','unknown')
                    AND updated_at <= ? ORDER BY updated_at, id LIMIT ?""",
                (cutoff, min(max(limit, 1), 500)),
            ).fetchall()
        return [self._request_row(row, destinations=[]) for row in rows]

    def requests_for(self, user_id: int, *, all_users: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM requests"
        parameters: tuple[object, ...] = ()
        if not all_users:
            query += " WHERE user_id=?"
            parameters = (user_id,)
        query += " ORDER BY id DESC LIMIT 100"
        with self._db() as db:
            rows = db.execute(query, parameters).fetchall()
            destinations = self._destinations(db, [int(row["id"]) for row in rows])
        return [
            self._request_row(row, destinations=destinations.get(int(row["id"]), []))
            for row in rows
        ]

    @staticmethod
    def _request_row(row: sqlite3.Row, *, destinations: list[int]) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "media_type": row["media_type"],
            "external_id": int(row["external_id"]),
            "seasons": json.loads(row["seasons"]),
            "title": row["title"],
            "year": row["year"],
            "user_id": int(row["user_id"]),
            "options": json.loads(row["options"]),
            "state": row["state"],
            "provider_status": row["provider_status"],
            "destinations": destinations,
            "created_at": int(row["created_at"]),
            "updated_at": int(row["updated_at"]),
            "fulfilled_at": row["fulfilled_at"],
        }

    @staticmethod
    def _destinations(database: sqlite3.Connection, request_ids: list[int]) -> dict[int, list[int]]:
        if not request_ids:
            return {}
        placeholders = ",".join("?" for _ in request_ids)
        rows = database.execute(
            f"""SELECT request_id, chat_id FROM request_destinations
            WHERE request_id IN ({placeholders}) ORDER BY created_at, chat_id""",
            request_ids,
        ).fetchall()
        result: dict[int, list[int]] = {}
        for row in rows:
            result.setdefault(int(row["request_id"]), []).append(int(row["chat_id"]))
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
                label = self._media_activity_label(
                    media_type=media_type,
                    title=title,
                    show_title=show_title,
                    season_number=season_number,
                    episode_number=episode_number,
                )
                db.execute(
                    """INSERT INTO activity(occurred_at, kind, user_id, label)
                    VALUES (?, ?, NULL, ?)""",
                    (now, "available", label[:200]),
                )
            return cursor.rowcount == 1

    @staticmethod
    def _media_activity_label(
        *,
        media_type: str,
        title: str,
        show_title: str | None,
        season_number: object,
        episode_number: object,
    ) -> str:
        if media_type == "movie":
            return f"Plex added movie · {title}"[:200]
        show = show_title or title
        if isinstance(season_number, int) and isinstance(episode_number, int):
            return f"Plex added {show} · S{season_number:02d}E{episode_number:02d} · {title}"[:200]
        if isinstance(season_number, int):
            return f"Plex added {show} · Season {season_number}"[:200]
        return f"Plex added series · {show}"[:200]

    def set_media_external_id(self, event_key: str, external_id: int) -> None:
        with self._db() as db:
            db.execute(
                "UPDATE media_events SET external_id=? WHERE event_key=?",
                (external_id, event_key),
            )

    def set_media_plex_url(self, event_key: str, plex_url: str) -> None:
        with self._db() as db:
            db.execute(
                "UPDATE media_events SET plex_url=? WHERE event_key=?",
                (plex_url, event_key),
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

    def request_destinations(
        self, *, media_type: str, external_id: int | None, season_number: int | None
    ) -> set[tuple[int, int]]:
        if external_id is None:
            return set()
        with self._db() as db:
            rows = db.execute(
                """SELECT requests.user_id, request_destinations.chat_id, requests.seasons
                FROM requests JOIN request_destinations
                    ON request_destinations.request_id=requests.id
                WHERE requests.media_type=? AND requests.external_id=?
                    AND requests.state IN ('pending','requested','unknown')""",
                (media_type, external_id),
            ).fetchall()
        destinations: set[tuple[int, int]] = set()
        for row in rows:
            seasons = json.loads(row["seasons"])
            if media_type == "movie" or season_number is None or season_number in seasons:
                destinations.add((int(row["user_id"]), int(row["chat_id"])))
        return destinations

    def requested_seasons(self, external_id: int) -> set[int]:
        """Return outstanding requested seasons for one series."""

        with self._db() as db:
            rows = db.execute(
                """SELECT seasons FROM requests
                WHERE media_type='series' AND external_id=?
                    AND state IN ('pending','requested','unknown')""",
                (external_id,),
            ).fetchall()
        result: set[int] = set()
        for row in rows:
            result.update(
                item for item in json.loads(row["seasons"]) if isinstance(item, int) and item > 0
            )
        return result

    def mark_movie_available(self, external_id: int) -> None:
        now = int(time.time())
        with self._db() as db:
            db.execute(
                """UPDATE requests SET state='available', provider_status='available',
                    updated_at=?, fulfilled_at=?
                WHERE media_type='movie' AND external_id=?
                    AND state IN ('pending','requested','unknown')""",
                (now, now, external_id),
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

    def request_page(self, number: int = 1, page_size: int = 25) -> Page:
        with self._db() as db:
            total = int(db.execute("SELECT count(*) FROM requests").fetchone()[0])
            current, pages, offset = self._page(number, page_size, total)
            rows = db.execute(
                """SELECT requests.*, users.username, users.first_name, users.last_name
                FROM requests LEFT JOIN users USING(user_id)
                ORDER BY requests.created_at DESC, requests.id DESC LIMIT ? OFFSET ?""",
                (min(max(page_size, 1), 100), offset),
            ).fetchall()
            destinations = self._destinations(db, [int(row["id"]) for row in rows])
        items = [
            {
                "id": int(row["id"]),
                "media_type": row["media_type"],
                "external_id": int(row["external_id"]),
                "seasons": json.loads(row["seasons"]),
                "title": row["title"],
                "year": row["year"],
                "user_id": int(row["user_id"]),
                "username": row["username"],
                "name": " ".join(part for part in (row["first_name"], row["last_name"]) if part)
                or None,
                "state": row["state"],
                "provider_status": row["provider_status"],
                "destinations": destinations.get(int(row["id"]), []),
                "created_at": int(row["created_at"]),
                "updated_at": int(row["updated_at"]),
            }
            for row in rows
        ]
        return Page(items, current, pages, total)

    def recent_requests(self, limit: int = 25) -> list[dict[str, Any]]:
        return self.request_page(1, limit).items
