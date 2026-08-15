from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from media_companion.db import Database
from media_companion.runtime_activation import (
    ActivationBlocked,
    DurablePlexActivation,
    LibraryTarget,
    ScanIntegrityError,
)


UTC = timezone.utc
BASE = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _database(tmp_path) -> Database:
    database = Database(tmp_path / "companion.sqlite3")
    database.migrate()
    return database


def _target(fetch_page):
    return LibraryTarget(
        "server-1", "library-1", library="library-1", fetch_page=fetch_page
    )


def test_two_pass_activation_is_historical_and_strictly_new(tmp_path) -> None:
    clock = FakeClock(BASE)
    phase = ["pass1"]
    pass_one = [
        {"ratingKey": "1", "addedAt": BASE - timedelta(days=1)},
    ]
    pass_two = pass_one + [
        {"ratingKey": "2", "addedAt": BASE + timedelta(seconds=1)},
        {"ratingKey": "3", "addedAt": BASE},
        {"ratingKey": "4"},
        {"ratingKey": "5", "addedAt": BASE + timedelta(seconds=2), "coarse": True},
    ]

    def fetch(_target, cursor, limit):
        values = pass_one if phase[0] == "pass1" else pass_two
        offset = int(cursor or "0")
        chunk = values[offset : offset + limit]
        return {
            "items": chunk,
            "cursor": str(offset),
            "next_cursor": str(offset + len(chunk))
            if offset + len(chunk) < len(values)
            else None,
            "total": len(values),
            "has_more": offset + len(chunk) < len(values),
        }

    database = _database(tmp_path)
    service = DurablePlexActivation(
        database, [_target(fetch)], clock=clock, page_size=2, items_per_run=20
    )
    first = service.run_pass(1)
    assert first.complete
    assert service.state().baseline_started_at == BASE

    phase[0] = "pass2"
    second = service.run_pass(2)
    assert second.complete
    with database.connection() as connection:
        rows = connection.execute(
            "SELECT rating_key,classification FROM runtime_activation_members "
            "WHERE scan_id IN (SELECT id FROM runtime_activation_scans WHERE phase='pass2') "
            "ORDER BY rating_key"
        ).fetchall()
    assert [(str(row[0]), str(row[1])) for row in rows] == [
        ("1", "historical"),
        ("2", "new"),
        ("3", "quarantined"),
        ("4", "quarantined"),
        ("5", "quarantined"),
    ]
    with database.connection() as connection:
        assert [
            str(row[0])
            for row in connection.execute(
                "SELECT rating_key FROM runtime_activation_quarantine ORDER BY rating_key"
            ).fetchall()
        ] == ["3", "4", "5"]
    assert (
        service.classify_observation(
            {
                "logical_key": "server-1:library-1:1:0",
                "addedAt": BASE + timedelta(days=1),
            }
        ).reason
        == "pass_one_member"
    )
    assert service.enable_delivery()
    assert service.ready()


def test_crash_restart_resumes_bounded_scan_over_five_thousand_items(tmp_path) -> None:
    clock = FakeClock(BASE)
    values = [
        {"ratingKey": str(number), "addedAt": BASE - timedelta(days=1)}
        for number in range(1, 5_101)
    ]

    def fetch(_target, cursor, limit):
        offset = int(cursor or "0")
        chunk = values[offset : offset + limit]
        return {
            "items": chunk,
            "cursor": str(offset),
            "next_cursor": str(offset + len(chunk))
            if offset + len(chunk) < len(values)
            else None,
            "total": len(values),
            "has_more": offset + len(chunk) < len(values),
        }

    database = _database(tmp_path)
    first = DurablePlexActivation(
        database, [_target(fetch)], clock=clock, page_size=100, items_per_run=500
    )
    partial = first.run_pass(1)
    assert not partial.complete and partial.processed == 500
    restarted = DurablePlexActivation(
        database, [_target(fetch)], clock=clock, page_size=100, items_per_run=500
    )
    result = partial
    while not result.complete:
        result = restarted.run_pass(1)
    assert result.processed == 100
    assert restarted.state().pass1_complete
    with database.connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM runtime_activation_members"
            ).fetchone()[0]
            == 5_100
        )


def test_incomplete_page_blocks_activation_and_readiness(tmp_path) -> None:
    clock = FakeClock(BASE)

    def fetch(_target, cursor, limit):
        return {
            "items": [{"ratingKey": "1", "addedAt": BASE}],
            "cursor": cursor,
            "complete": False,
        }

    database = _database(tmp_path)
    service = DurablePlexActivation(database, [_target(fetch)], clock=clock)
    with pytest.raises(ScanIntegrityError):
        service.run_pass(1)
    assert service.state().status == "blocked"
    assert not service.ready()
    with pytest.raises(ActivationBlocked):
        service.run_pass(1)


def test_full_diff_tombstones_only_after_complete_scan_and_readd_increments_generation(
    tmp_path,
) -> None:
    clock = FakeClock(BASE)
    values = [{"ratingKey": "7", "addedAt": BASE - timedelta(days=1)}]

    def fetch(_target, cursor, limit):
        offset = int(cursor or "0")
        chunk = values[offset : offset + limit]
        return {
            "items": chunk,
            "cursor": str(offset),
            "next_cursor": None,
            "total": len(values),
            "complete": True,
        }

    database = _database(tmp_path)
    service = DurablePlexActivation(
        database, [_target(fetch)], clock=clock, page_size=10
    )
    service.run_pass(1)
    service.run_pass(2)
    assert service.run_full_reconciliation().complete
    with database.connection() as connection:
        assert (
            connection.execute(
                "SELECT lifecycle_status FROM runtime_activation_identities WHERE rating_key='7'"
            ).fetchone()[0]
            == "active"
        )

    values.clear()
    clock.value = BASE + timedelta(days=1)
    assert service.run_full_reconciliation().complete
    with database.connection() as connection:
        row = connection.execute(
            "SELECT tombstone_generation,lifecycle_status FROM runtime_activation_identities WHERE rating_key='7' ORDER BY tombstone_generation DESC LIMIT 1"
        ).fetchone()
    assert tuple(row) == (0, "tombstone")

    values.append({"ratingKey": "7", "addedAt": BASE + timedelta(days=2)})
    clock.value = BASE + timedelta(days=2)
    assert service.run_full_reconciliation().complete
    with database.connection() as connection:
        rows = connection.execute(
            "SELECT tombstone_generation,lifecycle_status FROM runtime_activation_identities WHERE rating_key='7' ORDER BY tombstone_generation"
        ).fetchall()
    assert [tuple(row) for row in rows] == [(0, "tombstone"), (1, "active")]
