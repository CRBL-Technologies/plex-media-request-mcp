"""Numbered, transactional SQLite schema migrations."""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 2

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
        "options",
        "state",
        "provider_status",
        "created_at",
        "updated_at",
        "fulfilled_at",
    },
    "request_destinations": {"request_id", "chat_id", "created_at"},
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


def migrate(database: sqlite3.Connection) -> None:
    """Advance one database through each migration exactly once."""

    version = int(database.execute("PRAGMA user_version").fetchone()[0])
    if version < 0 or version > SCHEMA_VERSION:
        raise RuntimeError(f"unsupported gateway database version: {version}")
    existing = {
        str(row["name"])
        for row in database.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    if version == 0 and existing & TABLE_COLUMNS.keys():
        raise RuntimeError("gateway database has an unversioned incompatible schema")
    if version == 0:
        _migration_1(database)
        version = 1
    if version == 1:
        _migration_2(database)
        version = 2
    database.execute(f"PRAGMA user_version={version}")
    _validate(database)


def _migration_1(database: sqlite3.Connection) -> None:
    """Create the first clean-gateway schema as a migration baseline."""

    database.executescript(
        """
        PRAGMA journal_mode=WAL;
        BEGIN IMMEDIATE;
        CREATE TABLE users (
            user_id INTEGER PRIMARY KEY,
            chat_id INTEGER NOT NULL,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            first_seen INTEGER NOT NULL,
            last_seen INTEGER NOT NULL,
            last_blocked INTEGER
        );
        CREATE TABLE activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            occurred_at INTEGER NOT NULL,
            kind TEXT NOT NULL,
            user_id INTEGER,
            label TEXT NOT NULL
        );
        CREATE INDEX activity_recent ON activity(occurred_at DESC);
        CREATE TABLE requests (
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
        CREATE INDEX requests_external ON requests(media_type, external_id, state);
        CREATE TABLE media_events (
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
        CREATE TABLE deliveries (
            event_key TEXT NOT NULL,
            chat_id INTEGER NOT NULL,
            delivered_at INTEGER NOT NULL,
            PRIMARY KEY(event_key, chat_id)
        );
        PRAGMA user_version=1;
        COMMIT;
        """
    )


def _migration_2(database: sqlite3.Connection) -> None:
    """Separate durable acquisition intent from notification destinations."""

    database.executescript(
        """
        BEGIN IMMEDIATE;
        DELETE FROM activity WHERE kind='seen';
        ALTER TABLE requests RENAME TO requests_v1;
        DROP INDEX requests_external;
        CREATE TABLE requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            media_type TEXT NOT NULL CHECK(media_type IN ('movie','series')),
            external_id INTEGER NOT NULL,
            seasons TEXT NOT NULL DEFAULT '[]',
            title TEXT NOT NULL,
            year INTEGER,
            user_id INTEGER NOT NULL,
            options TEXT NOT NULL DEFAULT '{}',
            state TEXT NOT NULL CHECK(state IN ('pending','requested','available','unknown')),
            provider_status TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            fulfilled_at INTEGER,
            UNIQUE(media_type, external_id, seasons, user_id)
        );
        INSERT INTO requests(
            id, media_type, external_id, seasons, title, year, user_id,
            state, provider_status, created_at, updated_at, fulfilled_at
        )
        SELECT id, media_type, external_id, seasons, title, year, user_id,
            CASE WHEN state='available' THEN 'available' ELSE 'requested' END,
            CASE WHEN state='available' THEN 'available' ELSE 'legacy_requested' END,
            created_at, COALESCE(fulfilled_at, created_at), fulfilled_at
        FROM requests_v1;
        CREATE INDEX requests_external ON requests(media_type, external_id, state);
        CREATE TABLE request_destinations (
            request_id INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
            chat_id INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            PRIMARY KEY(request_id, chat_id)
        );
        INSERT INTO request_destinations(request_id, chat_id, created_at)
        SELECT id, chat_id, created_at FROM requests_v1;
        DROP TABLE requests_v1;
        PRAGMA user_version=2;
        COMMIT;
        """
    )


def _validate(database: sqlite3.Connection) -> None:
    for table, expected in TABLE_COLUMNS.items():
        columns = {
            str(row["name"]) for row in database.execute(f'PRAGMA table_info("{table}")').fetchall()
        }
        if columns != expected:
            raise RuntimeError(f"gateway database table is incompatible: {table}")
