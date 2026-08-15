from __future__ import annotations

from datetime import datetime, timedelta, timezone
import tempfile
from pathlib import Path

from media_companion.db import Database
from media_companion.models import MediaType, RequestMode
from media_companion.planner import CanonicalUnit, Subscription
from media_companion.runtime_notifications import (
    DeliveryOutcome,
    DurableNotificationRepository,
)


UTC = timezone.utc
BASE = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


def unit(
    key: str,
    offset: int,
    *,
    media_type: MediaType = MediaType.EPISODE,
    season: int = 1,
    episode: int = 1,
    title: str | None = None,
    show: str = "show-1",
) -> CanonicalUnit:
    return CanonicalUnit(
        unit_id=key,
        media_type=media_type,
        visible_in_plex_at=BASE + timedelta(seconds=offset),
        title=title or key,
        show_identity=None if media_type is MediaType.MOVIE else show,
        season_number=None if media_type is MediaType.MOVIE else season,
        episode_number=None if media_type is MediaType.MOVIE else episode,
        plex_url="https://plex.example/library/metadata/1",
        server_uuid="server-1",
        library_uuid="library-1",
        snapshot_verified=True,
        playable=True,
    )


def repository() -> DurableNotificationRepository:
    database = Database(Path(tempfile.mkdtemp()) / "notifications.sqlite")
    database.migrate()
    return DurableNotificationRepository(database, clock=lambda: BASE)


class Telegram:
    def __init__(
        self, result: object | None = None, error: BaseException | None = None
    ) -> None:
        self.result = result if result is not None else {"ok": True, "message_id": 42}
        self.error = error
        self.calls: list[tuple[int, str]] = []

    def send_message(self, chat_id: int, text: str, **_: object) -> object:
        self.calls.append((chat_id, text))
        if self.error is not None:
            raise self.error
        return self.result


def test_admin_post_activation_and_historical_filter_are_idempotent() -> None:
    repo = repository()
    old = unit("old", -900, media_type=MediaType.MOVIE)
    new = unit("new", -500, media_type=MediaType.MOVIE)
    first = repo.plan(
        [old, new],
        admin_destinations=[-100],
        activation_started_at=BASE - timedelta(minutes=10),
        now=BASE,
        scan_complete=True,
        full_sweep_complete=True,
        scans_fresh=True,
    )
    assert [row.unit_key for row in first.obligations] == ["new"]
    assert len(repo.list_deliveries()) == 1

    second = repo.plan(
        [old, new],
        admin_destinations=[-100],
        activation_started_at=BASE - timedelta(minutes=10),
        now=BASE,
        scan_complete=True,
        full_sweep_complete=True,
        scans_fresh=True,
    )
    assert second.delivery_ids == ()
    assert len(repo.list_deliveries()) == 1


def test_requester_is_plex_visible_and_same_chat_uses_admin_winner() -> None:
    repo = repository()
    item = unit("episode", -600, episode=4)
    subscription = Subscription(
        generation=1,
        subscription_id="sub-1",
        destination=-100,
        user_id=7,
        active_at=BASE - timedelta(hours=1),
        unit_keys=frozenset({"episode"}),
        mode=RequestMode.AIRING_EPISODE,
    )
    result = repo.plan(
        [item],
        subscriptions=[subscription],
        admin_destinations=[-100],
        now=BASE,
        scan_complete=True,
        full_sweep_complete=True,
        scans_fresh=True,
    )
    assert len(result.obligations) == 2
    assert {row.state for row in result.obligations} == {"pending", "suppressed"}
    repo.materialize_due(now=BASE)
    telegram = Telegram()
    attempts = repo.deliver_due(telegram, now=BASE)
    assert attempts[0].outcome is DeliveryOutcome.SENT
    assert len(telegram.calls) == 1
    assert "S01E04" in telegram.calls[0][1]


def test_airing_episodes_split_by_week_and_bulk_season_is_one_group() -> None:
    repo = repository()
    first = unit("e1", -900, episode=1)
    second = unit("e2", -500, episode=2)
    subscription = Subscription(
        generation=1,
        subscription_id="airing",
        destination=9,
        user_id=9,
        active_at=BASE - timedelta(hours=1),
        unit_keys=frozenset({"e1", "e2"}),
        mode=RequestMode.AIRING_EPISODE,
    )
    plan = repo.plan(
        [first, second],
        subscriptions=[subscription],
        now=BASE,
        scan_complete=True,
        full_sweep_complete=True,
        scans_fresh=True,
        materialize_due=False,
    )
    assert len(plan.groups) == 2

    repo2 = repository()
    bulk_first = unit("e1", -600, episode=1)
    bulk_second = unit("e2", -500, episode=2)
    completion = Subscription(
        generation=1,
        subscription_id="season",
        destination=9,
        user_id=9,
        active_at=BASE - timedelta(hours=1),
        unit_keys=frozenset({"e1", "e2"}),
        required_unit_keys=frozenset({"e1", "e2"}),
        enumeration_complete=True,
        season_ended=True,
        mode=RequestMode.SEASON_COMPLETION,
    )
    repo2.plan(
        [bulk_first, bulk_second],
        subscriptions=[completion],
        now=BASE,
        scan_complete=True,
        full_sweep_complete=True,
        scans_fresh=True,
        materialize_due=False,
    )
    ids = repo2.materialize_due(now=BASE)
    assert len(ids) == 1


def test_send_retry_schedule_and_restart_converge() -> None:
    repo = repository()
    item = unit("movie", -600, media_type=MediaType.MOVIE)
    repo.plan([item], admin_destinations=[1], now=BASE, materialize_due=True)

    class RetryTelegram:
        def send_message(self, *_: object, **__: object) -> object:
            error = RuntimeError("not transmitted")
            error.error_class = "retryable"  # type: ignore[attr-defined]
            raise error

    attempt = repo.deliver_due(RetryTelegram(), now=BASE)
    assert attempt[0].outcome is DeliveryOutcome.RETRY_WAIT
    view = repo.list_deliveries()[0]
    assert view.retry_due_at == BASE + timedelta(minutes=1)
    # A fresh repository sees the same durable obligation and delivery.
    restarted = DurableNotificationRepository(
        repo.database, clock=lambda: BASE + timedelta(minutes=1)
    )
    assert len(restarted.list_deliveries()) == 1
    assert restarted.oracle(
        scan_complete=True, full_sweep_complete=True, scans_fresh=True
    ).ready


def test_incomplete_oracle_never_passes_and_unknown_recovery_is_explicit() -> None:
    repo = repository()
    item = unit("movie", -600, media_type=MediaType.MOVIE)
    repo.plan([item], admin_destinations=[1], now=BASE, materialize_due=True)

    class AmbiguousTelegram:
        def send_message(self, *_: object, **__: object) -> object:
            error = RuntimeError("transport timeout")
            error.error_class = "ambiguous"  # type: ignore[attr-defined]
            error.transmitted = True  # type: ignore[attr-defined]
            raise error

    # It is not unknown while the finite send deadline/grace is still open.
    attempt = repo.deliver_due(AmbiguousTelegram(), now=BASE)
    assert attempt[0].outcome is DeliveryOutcome.SKIPPED
    assert repo.oracle().ready is False
    repo.expire_sending(now=BASE + timedelta(seconds=31))
    delivery_id = repo.list_deliveries()[0].delivery_id
    assert repo.manual_action(
        delivery_id, "assume_sent", confirmed=True, now=BASE + timedelta(seconds=31)
    )["ok"]
    assert repo.oracle(
        scan_complete=True, full_sweep_complete=True, scans_fresh=True
    ).ready
