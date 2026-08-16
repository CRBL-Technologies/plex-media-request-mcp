from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from media_gateway.legacy import apply_legacy, backup_database, plan_legacy
from media_gateway.store import Store


def _legacy(path: Path, rows: list[tuple[object, ...]]) -> None:
    with sqlite3.connect(path) as database:
        database.execute(
            """CREATE TABLE media_requests (
                id INTEGER PRIMARY KEY,
                media_type TEXT,
                title TEXT,
                year INTEGER,
                requested_by_user_id INTEGER,
                requested_by_chat_id INTEGER,
                requested_by_username TEXT,
                tmdb_id INTEGER,
                tvdb_id INTEGER,
                season_numbers TEXT,
                status TEXT,
                created_at TEXT,
                notified_available_at TEXT
            )"""
        )
        database.executemany(
            "INSERT INTO media_requests VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


def test_legacy_plan_recovers_username_and_expands_ambiguous_actor(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.sqlite3"
    _legacy(
        legacy,
        [
            (
                1,
                "movie",
                "Old Alice",
                2020,
                10,
                10,
                "Alice",
                1,
                None,
                None,
                "available",
                "2026-01-01T00:00:00+00:00",
                "2026-01-02T00:00:00+00:00",
            ),
            (
                2,
                "movie",
                "Old Shared A",
                2020,
                40,
                40,
                "Shared",
                2,
                None,
                None,
                "available",
                "2026-01-01T00:00:00+00:00",
                "2026-01-02T00:00:00+00:00",
            ),
            (
                3,
                "movie",
                "Old Shared B",
                2020,
                41,
                41,
                "Shared",
                3,
                None,
                None,
                "available",
                "2026-01-01T00:00:00+00:00",
                "2026-01-02T00:00:00+00:00",
            ),
            (
                4,
                "movie",
                "Direct",
                2026,
                30,
                30,
                "Direct",
                100,
                None,
                None,
                "requested",
                "2026-02-01T00:00:00+00:00",
                None,
            ),
            (
                5,
                "movie",
                "Alice Request",
                2026,
                None,
                None,
                "alice",
                101,
                None,
                None,
                "requested",
                "2026-02-02T00:00:00+00:00",
                None,
            ),
            (
                6,
                "series",
                "Shared Request",
                2026,
                None,
                None,
                "SHARED",
                None,
                102,
                "[1]",
                "requested",
                "2026-02-03T00:00:00+00:00",
                None,
            ),
            (
                7,
                "movie",
                "Unknown",
                2026,
                None,
                None,
                None,
                103,
                None,
                None,
                "requested",
                "2026-02-04T00:00:00+00:00",
                None,
            ),
        ],
    )

    plan = plan_legacy(legacy)

    assert plan.pending_rows == 4
    assert plan.direct_rows == 1
    assert plan.username_rows == 2
    assert plan.fanout_rows == 1
    assert plan.mapped_source_rows == 3
    assert plan.unresolved_rows == 1
    assert plan.invalid_rows == 0
    assert len(plan.records) == 4
    assert {record.user_id for record in plan.records} == {10, 30, 40, 41}


def test_legacy_apply_is_backed_up_transactional_and_idempotent(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.sqlite3"
    target = tmp_path / "gateway.sqlite3"
    backup = tmp_path / "gateway.before-import.sqlite3"
    _legacy(
        legacy,
        [
            (
                1,
                "movie",
                "Movie",
                2026,
                10,
                10,
                "user",
                100,
                None,
                None,
                "requested",
                "2026-02-01T00:00:00+00:00",
                None,
            ),
        ],
    )
    Store(target)
    plan = plan_legacy(legacy)

    digest = backup_database(target, backup)
    first = apply_legacy(target, plan)
    second = apply_legacy(target, plan)

    assert len(digest) == 64
    assert backup.is_file()
    assert first["inserted"] == 1
    assert second["inserted"] == 0
    assert second["already_present"] == 1
    page = Store(target).request_page()
    assert page.total == 1
    assert page.items[0]["username"] == "user"


def test_invalid_pending_legacy_row_fails_before_import(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.sqlite3"
    target = tmp_path / "gateway.sqlite3"
    _legacy(
        legacy,
        [
            (
                1,
                "movie",
                "Broken",
                2026,
                10,
                10,
                "user",
                None,
                None,
                None,
                "requested",
                "2026-02-01T00:00:00+00:00",
                None,
            ),
        ],
    )
    Store(target)
    plan = plan_legacy(legacy)

    assert plan.invalid_rows == 1
    with pytest.raises(ValueError, match="invalid pending rows"):
        apply_legacy(target, plan)
    assert Store(target).request_page().total == 0


def test_legacy_source_and_backup_reject_symlinks(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.sqlite3"
    target = tmp_path / "gateway.sqlite3"
    _legacy(legacy, [])
    Store(target)
    legacy_link = tmp_path / "legacy-link.sqlite3"
    backup_link = tmp_path / "backup-link.sqlite3"
    legacy_link.symlink_to(legacy)
    backup_link.symlink_to(tmp_path / "missing.sqlite3")

    with pytest.raises(ValueError, match="cannot be a symlink"):
        plan_legacy(legacy_link)
    with pytest.raises(ValueError, match="cannot be a symlink"):
        backup_database(target, backup_link)
