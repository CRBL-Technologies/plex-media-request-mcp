from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

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
    store.record_request(
        media_type="movie",
        external_id=123,
        seasons=(),
        title="Movie",
        year=2026,
        actor=Actor(user_id=1001, chat_id=1001),
    )
    store.mark_movie_available(123)

    store.prune(now=old + 61 * 24 * 60 * 60)

    assert store.pending_media_events(old + 10_000) == []
    assert store.requests_for(1001) == []


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
