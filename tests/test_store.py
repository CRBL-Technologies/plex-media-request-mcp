from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from media_gateway import migrations
from media_gateway.migrations import _migration_1, _migration_2
from media_gateway.store import Store
from media_gateway.types import Actor


def test_prune_removes_terminal_and_unresolved_operational_data(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    old = int(time.time())
    store.add_media_event(
        event_key="episode:old",
        media_type="series",
        external_id=None,
        rating_key="1",
        title="Episode",
        show_title="Show",
        season_number=1,
        episode_number=1,
        plex_url="https://app.plex.tv/1",
        observed_at=old,
    )
    store.mark_delivered(["episode:old"], 9001)
    store.mark_delivered(["season-complete:123:1:library:10"], 9001)
    store.record_request(
        media_type="movie",
        external_id=123,
        seasons=(),
        title="Movie",
        year=2026,
        actor=Actor(user_id=1001, chat_id=1001),
    )
    store.mark_movie_available(123, store.request_cycles(media_type="movie", external_id=123))
    store.prune(now=old + 61 * 24 * 60 * 60)

    assert store.pending_media_events(old + 10_000) == []
    assert store.requests_for(1001) == []
    assert not store.delivered(["season-complete:123:1:library:10"], 9001)


def test_unversioned_conflicting_database_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    with sqlite3.connect(path) as database:
        database.execute("CREATE TABLE users (user_id INTEGER PRIMARY KEY)")

    with pytest.raises(RuntimeError, match="unversioned incompatible schema"):
        Store(path)


def test_unknown_database_version_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    with sqlite3.connect(path) as database:
        database.execute("PRAGMA user_version=99")

    with pytest.raises(RuntimeError, match="unsupported gateway database version: 99"):
        Store(path)


def test_database_context_rolls_back_on_error(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")

    with pytest.raises(RuntimeError, match="stop"), store._db() as database:
        database.execute(
            """INSERT INTO activity(occurred_at, kind, user_id, label)
            VALUES (1, 'test', NULL, 'No')"""
        )
        raise RuntimeError("stop")

    assert store.recent_activity() == []


def test_activity_cleanup_and_pagination(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    actor = Actor(user_id=1001, chat_id=1001)
    store.observe_actor(actor)
    for number in range(30):
        store.record_activity("request", f"Request {number}", actor.user_id)

    page = store.activity_page(2, 10)

    assert page.number == 2
    assert page.pages == 3
    assert page.total == 30
    assert len(page.items) == 10
    assert all(item["label"] != "Active user" for item in page.items)


def test_v1_migration_preserves_request_and_moves_chat_to_destination(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    with sqlite3.connect(path) as database:
        database.row_factory = sqlite3.Row
        _migration_1(database)
        database.execute(
            """INSERT INTO requests(
                media_type, external_id, seasons, title, year,
                user_id, chat_id, state, created_at
            ) VALUES ('movie', 123, '[]', 'Movie', 2026, 1001, -10001, 'requested', 50)"""
        )
        database.execute(
            """INSERT INTO activity(occurred_at, kind, user_id, label)
            VALUES (50, 'seen', 1001, 'Active user')"""
        )

    store = Store(path)

    request = store.requests_for(1001)[0]
    assert request["state"] == "requested"
    assert request["provider_status"] == "legacy_requested"
    assert request["destinations"] == [-10001]
    assert store.recent_activity() == []
    with sqlite3.connect(path) as database:
        assert database.execute("PRAGMA user_version").fetchone()[0] == 3


def test_v2_migration_preserves_state_and_backfills_available_series(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    with sqlite3.connect(path) as database:
        database.row_factory = sqlite3.Row
        _migration_1(database)
        _migration_2(database)
        database.execute(
            """INSERT INTO requests(
                id, media_type, external_id, seasons, title, year, user_id, options,
                state, provider_status, created_at, updated_at, fulfilled_at
            ) VALUES (7, 'series', 123, '[1,2]', 'Series', 2026, 1001, '{"anime":false}',
                'available', 'available', 40, 50, 50)"""
        )
        database.execute("INSERT INTO request_destinations VALUES (7, -10001, 40)")
        database.execute("INSERT INTO deliveries VALUES ('episode:kept', -10001, 50)")

    first = Store(path)
    intent = first.request_intent(7)
    second = Store(path)

    assert intent is not None
    assert intent["generation"] == 1
    assert intent["fulfilled_seasons"] == [1, 2]
    assert intent["created_at"] == 40
    assert intent["options"] == {"anime": False}
    assert intent["destinations"] == [-10001]
    assert second.delivered(["episode:kept"], -10001)
    with sqlite3.connect(path) as database:
        assert database.execute("PRAGMA user_version").fetchone()[0] == 3


def test_v3_validation_failure_rolls_back_additive_columns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.sqlite3"
    with sqlite3.connect(path) as database:
        database.row_factory = sqlite3.Row
        _migration_1(database)
        _migration_2(database)

    def fail(_database: sqlite3.Connection) -> None:
        raise RuntimeError("validation failed")

    monkeypatch.setattr(migrations, "_validate", fail)
    with pytest.raises(RuntimeError, match="validation failed"):
        Store(path)

    with sqlite3.connect(path) as database:
        columns = {
            str(row[1]) for row in database.execute("PRAGMA table_info(requests)").fetchall()
        }
        assert database.execute("PRAGMA user_version").fetchone()[0] == 2
        assert "generation" not in columns
        assert "fulfilled_seasons" not in columns


def test_same_user_request_from_two_chats_preserves_both_destinations(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    for chat_id in (1001, -10001):
        store.record_request(
            media_type="movie",
            external_id=123,
            seasons=(),
            title="Movie",
            year=2026,
            actor=Actor(user_id=1001, chat_id=chat_id),
        )

    requests = store.requests_for(1001)

    assert len(requests) == 1
    assert set(requests[0]["destinations"]) == {1001, -10001}
    assert store.request_destinations(media_type="movie", external_id=123, season_number=None) == {
        (1001, 1001),
        (1001, -10001),
    }


def test_request_intent_reads_current_state_and_destinations(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    request_id = store.record_request(
        media_type="series",
        external_id=123,
        seasons=(1,),
        title="Series",
        year=2026,
        actor=Actor(user_id=1001, chat_id=-10001),
    )

    intent = store.request_intent(request_id)

    assert intent is not None
    assert intent["state"] == "requested"
    assert intent["destinations"] == [-10001]
    assert store.request_intent(request_id + 1) is None
    assert "generation" not in store.requests_for(1001)[0]
    assert "fulfilled_seasons" not in store.requests_for(1001)[0]


def test_available_series_request_remains_a_notification_subscription(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    request_id = store.record_request(
        media_type="series",
        external_id=123,
        seasons=(1,),
        title="Series",
        year=2026,
        actor=Actor(user_id=1001, chat_id=1001),
    )
    store.complete_request(request_id, "available", generation=1)

    assert store.requested_seasons(123) == set()
    assert store.request_destinations(media_type="series", external_id=123, season_number=1) == {
        (1001, 1001)
    }


def test_request_generation_rejects_stale_completion_and_preserves_creation_time(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state.sqlite3")
    actor = Actor(user_id=1001, chat_id=1001)
    request_id = store.begin_request(
        media_type="movie",
        external_id=123,
        seasons=(),
        title="Movie",
        year=2026,
        actor=actor,
    )
    first = store.request_intent(request_id)
    assert first is not None
    first_cycles = store.request_cycles(media_type="movie", external_id=123)
    store.begin_request(
        media_type="movie",
        external_id=123,
        seasons=(),
        title="Movie",
        year=2026,
        actor=actor,
    )
    second = store.request_intent(request_id)
    assert second is not None

    assert second["generation"] == 2
    assert second["created_at"] == first["created_at"]
    assert not store.complete_request(request_id, "available", generation=1)
    assert not store.mark_request_unknown(request_id, generation=1)
    store.mark_movie_available(123, first_cycles)
    assert store.request_intent(request_id)["state"] == "pending"  # type: ignore[index]
    assert store.complete_request(request_id, "available", generation=2)
    assert store.complete_request(request_id, "requested", generation=2)
    assert store.request_intent(request_id)["state"] == "available"  # type: ignore[index]


def test_series_seasons_fulfill_only_when_the_selected_set_is_verified(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    request_id = store.record_request(
        media_type="series",
        external_id=123,
        seasons=(1, 2),
        title="Series",
        year=2026,
        actor=Actor(user_id=1001, chat_id=1001),
    )
    cycles = store.request_cycles(media_type="series", external_id=123, season_number=2)

    store.mark_series_seasons_available(cycles, {1})
    partial = store.request_intent(request_id)
    assert partial is not None
    assert partial["state"] == "requested"
    assert partial["fulfilled_seasons"] == [1]
    assert store.requested_seasons(123) == {2}

    store.mark_series_seasons_available(cycles, {2})
    complete = store.request_intent(request_id)
    assert complete is not None
    assert complete["state"] == "available"
    assert complete["fulfilled_seasons"] == [1, 2]
    assert store.requested_seasons(123) == set()


def test_failed_numbered_migration_rolls_back_its_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.sqlite3"
    with sqlite3.connect(path) as database:
        database.row_factory = sqlite3.Row
        _migration_1(database)

    def fail(database: sqlite3.Connection) -> None:
        database.executescript("BEGIN IMMEDIATE; CREATE TABLE incomplete(value INTEGER);")
        raise RuntimeError("migration failed")

    monkeypatch.setattr(migrations, "_migration_2", fail)
    with pytest.raises(RuntimeError, match="migration failed"):
        Store(path)

    with sqlite3.connect(path) as database:
        assert database.execute("PRAGMA user_version").fetchone()[0] == 1
        assert (
            database.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='incomplete'"
            ).fetchone()[0]
            == 0
        )
