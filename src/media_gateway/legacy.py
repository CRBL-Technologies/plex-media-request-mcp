"""Small, explicit importer for the preserved pre-rewrite request database."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import time
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .store import Store

LEGACY_COLUMNS = {
    "id",
    "media_type",
    "title",
    "year",
    "requested_by_user_id",
    "requested_by_chat_id",
    "requested_by_username",
    "tmdb_id",
    "tvdb_id",
    "season_numbers",
    "status",
    "created_at",
    "notified_available_at",
}
PENDING_STATES = {"requested", "notifying"}


@dataclass(frozen=True, slots=True)
class ImportRecord:
    media_type: str
    external_id: int
    seasons: str
    title: str
    year: int | None
    user_id: int
    chat_id: int
    username: str | None
    created_at: int

    @property
    def key(self) -> tuple[str, int, str, int]:
        return self.media_type, self.external_id, self.seasons, self.user_id


@dataclass(frozen=True, slots=True)
class LegacyPlan:
    source_sha256: str
    source_rows: int
    pending_rows: int
    direct_rows: int
    username_rows: int
    fanout_rows: int
    mapped_source_rows: int
    unresolved_rows: int
    invalid_rows: int
    expanded_records: int
    duplicate_records: int
    records: tuple[ImportRecord, ...]

    def public(self) -> dict[str, Any]:
        value = asdict(replace(self, records=()))
        value.pop("records")
        value["subscriptions"] = len(self.records)
        return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _positive(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _chat(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value != 0 else None


def _username(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().casefold()


def _display_username(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    username = value.strip().removeprefix("@").strip()
    return username[:128] or None


def _created_at(value: object) -> int:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("missing creation timestamp")
    text = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        # The legacy service ran in UTC but its oldest rows used naive ISO text.
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


def _seasons(media_type: str, value: object) -> str:
    if media_type == "movie":
        return "[]"
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError as exc:
        raise ValueError("invalid season list") from exc
    if not isinstance(decoded, list):
        raise ValueError("invalid season list")
    seasons = sorted(
        {
            season
            for season in decoded
            if isinstance(season, int) and not isinstance(season, bool) and season > 0
        }
    )
    if len(seasons) != len(decoded) or not seasons:
        raise ValueError("invalid season list")
    return json.dumps(seasons, separators=(",", ":"))


def _record(row: sqlite3.Row, user_id: int, chat_id: int) -> ImportRecord:
    media_type = row["media_type"]
    if media_type not in {"movie", "series"}:
        raise ValueError("invalid media type")
    external_id = _positive(row["tmdb_id"] if media_type == "movie" else row["tvdb_id"])
    title = row["title"].strip() if isinstance(row["title"], str) else ""
    year = _positive(row["year"])
    if external_id is None or not title or len(title) > 300:
        raise ValueError("invalid media identity")
    return ImportRecord(
        media_type=media_type,
        external_id=external_id,
        seasons=_seasons(media_type, row["season_numbers"]),
        title=title,
        year=year,
        user_id=user_id,
        chat_id=chat_id,
        username=_display_username(row["requested_by_username"]),
        created_at=_created_at(row["created_at"]),
    )


def plan_legacy(source: Path) -> LegacyPlan:
    if source.is_symlink():
        raise ValueError("legacy database cannot be a symlink")
    path = source.resolve()
    if not path.is_file():
        raise ValueError("legacy database must be a regular file")
    wal = path.with_name(f"{path.name}-wal")
    if wal.is_file() and wal.stat().st_size:
        raise ValueError("legacy database has an uncheckpointed WAL")
    before = path.stat()
    source_sha256 = _sha256(path)
    uri = f"{path.as_uri()}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as database:
        database.row_factory = sqlite3.Row
        if database.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("legacy database integrity check failed")
        table = database.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='media_requests'"
        ).fetchone()
        if table is None:
            raise ValueError("legacy media_requests table is missing")
        columns = {
            str(row["name"]) for row in database.execute("PRAGMA table_info(media_requests)")
        }
        if not columns >= LEGACY_COLUMNS:
            raise ValueError("legacy media_requests schema is incompatible")
        rows = database.execute("SELECT * FROM media_requests ORDER BY id").fetchall()
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ) or source_sha256 != _sha256(path):
        raise ValueError("legacy database changed during inspection")

    identities: dict[str, set[tuple[int, int]]] = {}
    for row in rows:
        username = _username(row["requested_by_username"])
        user_id = _positive(row["requested_by_user_id"])
        chat_id = _chat(row["requested_by_chat_id"])
        if username and user_id is not None and chat_id is not None:
            identities.setdefault(username, set()).add((user_id, chat_id))

    pending = [row for row in rows if row["notified_available_at"] is None]
    direct = username_rows = fanout = mapped = unresolved = invalid = expanded = 0
    candidates: list[ImportRecord] = []
    for row in pending:
        if row["status"] not in PENDING_STATES:
            invalid += 1
            continue
        user_id = _positive(row["requested_by_user_id"])
        chat_id = _chat(row["requested_by_chat_id"])
        actors: set[tuple[int, int]]
        if user_id is not None and chat_id is not None:
            actors = {(user_id, chat_id)}
            direct += 1
        elif user_id is not None or chat_id is not None:
            invalid += 1
            continue
        else:
            username = _username(row["requested_by_username"])
            actors = identities.get(username, set()) if username else set()
            if not actors:
                unresolved += 1
                continue
            username_rows += 1
            if len(actors) > 1:
                fanout += 1
        try:
            records = [_record(row, actor_user, actor_chat) for actor_user, actor_chat in actors]
        except (TypeError, ValueError, OverflowError):
            invalid += 1
            continue
        mapped += 1
        expanded += len(records)
        candidates.extend(records)

    unique: dict[tuple[str, int, str, int], ImportRecord] = {}
    conflicting = 0
    for record in candidates:
        existing = unique.get(record.key)
        if existing is not None and existing.chat_id != record.chat_id:
            conflicting += 1
            continue
        if existing is None or record.created_at < existing.created_at:
            unique[record.key] = record
    if conflicting:
        invalid += conflicting
    unique_records = tuple(sorted(unique.values(), key=lambda item: (item.created_at, item.key)))
    return LegacyPlan(
        source_sha256=source_sha256,
        source_rows=len(rows),
        pending_rows=len(pending),
        direct_rows=direct,
        username_rows=username_rows,
        fanout_rows=fanout,
        mapped_source_rows=mapped,
        unresolved_rows=unresolved,
        invalid_rows=invalid,
        expanded_records=expanded,
        duplicate_records=expanded - len(unique_records),
        records=unique_records,
    )


def backup_database(source: Path, destination: Path) -> str:
    if source.is_symlink():
        raise ValueError("gateway database cannot be a symlink")
    source = source.resolve()
    if destination.is_symlink():
        raise ValueError("backup destination cannot be a symlink")
    destination = destination.resolve()
    if source == destination or destination.exists():
        raise ValueError("backup destination must be a new file")
    destination.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        created = True
        os.close(descriptor)
        with (
            sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True) as live,
            sqlite3.connect(destination) as backup,
        ):
            live.backup(backup)
            if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("new gateway backup failed integrity check")
        os.chmod(destination, 0o600)
        with destination.open("rb") as handle:
            os.fsync(handle.fileno())
        directory = os.open(destination.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return _sha256(destination)
    except Exception:
        if created:
            destination.unlink(missing_ok=True)
        raise


def apply_legacy(database_path: Path, plan: LegacyPlan) -> dict[str, Any]:
    if plan.invalid_rows:
        raise ValueError("legacy plan contains invalid pending rows")
    Store(database_path)
    inserted = 0
    with sqlite3.connect(database_path, timeout=10) as database:
        database.execute("BEGIN IMMEDIATE")
        for record in plan.records:
            database.execute(
                """INSERT INTO users(
                    user_id, chat_id, username, first_name, last_name,
                    first_seen, last_seen, last_blocked
                ) VALUES (?, ?, ?, NULL, NULL, ?, ?, NULL)
                ON CONFLICT(user_id) DO UPDATE SET
                    username=COALESCE(users.username, excluded.username),
                    first_seen=min(users.first_seen, excluded.first_seen)""",
                (
                    record.user_id,
                    record.chat_id,
                    record.username,
                    record.created_at,
                    record.created_at,
                ),
            )
            cursor = database.execute(
                """INSERT INTO requests(
                    media_type, external_id, seasons, title, year,
                    user_id, chat_id, state, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'requested', ?)
                ON CONFLICT(media_type, external_id, seasons, user_id) DO NOTHING""",
                (
                    record.media_type,
                    record.external_id,
                    record.seasons,
                    record.title,
                    record.year,
                    record.user_id,
                    record.chat_id,
                    record.created_at,
                ),
            )
            inserted += cursor.rowcount
        if inserted:
            database.execute(
                """INSERT INTO activity(occurred_at, kind, user_id, label)
                VALUES (?, 'migration', NULL, ?)""",
                (int(time.time()), f"Recovered {inserted} legacy requester subscriptions"),
            )
    return {
        **plan.public(),
        "inserted": inserted,
        "already_present": len(plan.records) - inserted,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--legacy", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup", type=Path)
    arguments = parser.parse_args()
    plan = plan_legacy(arguments.legacy)
    if not arguments.apply:
        print(json.dumps(plan.public(), sort_keys=True))
        return
    if arguments.backup is None:
        parser.error("--backup is required with --apply")
    backup_sha256 = backup_database(arguments.database, arguments.backup)
    result = apply_legacy(arguments.database, plan)
    result["backup_sha256"] = backup_sha256
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
