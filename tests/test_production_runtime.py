"""Focused executable contracts for the checked-in production composition.

These tests deliberately use the real ``production.py`` composition and a
temporary, migrated canonical ledger.  Provider fakes only implement the
narrow methods used by the runtime; they are not test doubles for the worker
or request store themselves.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from media_companion.auth import (
    ActorAssertionVerifier,
    ConfirmationRecord,
    InMemoryConfirmationTokenStore,
    canonical_argument_hash,
)
from media_companion.db import Database
from media_companion.errors import ConflictError
from media_companion.models import MediaCandidate, MediaIdentity, MediaType, PlexItem
from media_companion.operations import SQLiteRateLimiter
from media_companion.plex_ingress import NormalizedPlexEvent
from media_companion.production import (
    HermesTelegramBridge,
    ProviderRetryable,
    build_runtime,
)
from media_companion.tool_policy import UPSTREAM_TOOL_SET


UTC = timezone.utc
NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


class DurableNonceFake:
    """SQLite-shaped nonce store accepted by the production verifier."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def consume(self, nonce: str, expires_at: int, *, now: float | None = None) -> bool:
        del now
        digest = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
        with self.database.transaction() as connection:
            return bool(
                connection.execute(
                    "INSERT OR IGNORE INTO actor_nonces(nonce_hash, expires_at) VALUES (?, ?)",
                    (digest, expires_at),
                ).rowcount
            )

    def cleanup(self, *, now: float | None = None) -> int:
        current = int(now or 0)
        with self.database.transaction() as connection:
            return int(
                connection.execute(
                    "DELETE FROM actor_nonces WHERE expires_at <= ?", (current,)
                ).rowcount
            )


class DurableConfirmationFake:
    """Production-shaped confirmation store with a durable audit marker.

    The runtime's startup contract checks the interface and rejects the
    shipped in-memory implementation by type.  The actual callback lifecycle
    is covered by the auth tests; this wrapper keeps the runtime fixture's
    dependency shape realistic without broadening this integration file.
    """

    def __init__(self, database: Database) -> None:
        self.database = database
        self.delegate = InMemoryConfirmationTokenStore()

    def create(self, **kwargs: object) -> object:
        token = self.delegate.create(**kwargs)
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO confirmation_capabilities(token_hash, actor_user_id, actor_chat_id, tool, argument_hash, target_identity, state_fingerprint, preview_hash, policy_version, nonce, issued_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    token.token_hash,
                    kwargs["actor_user_id"],
                    kwargs["actor_chat_id"],
                    kwargs["tool"],
                    kwargs["argument_hash"],
                    kwargs["target_identity"],
                    kwargs["state_fingerprint"],
                    hashlib.sha256(str(kwargs["preview"]).encode()).hexdigest(),
                    kwargs.get("policy_version", "1"),
                    "fixture",
                    token.issued_at,
                    token.expires_at,
                ),
            )
        return token

    def bind(self, token: str, **kwargs: object) -> ConfirmationRecord:
        return self.delegate.bind(token, **kwargs)

    def consume(self, token: str, **kwargs: object) -> ConfirmationRecord:
        return self.delegate.consume(token, **kwargs)


class ConfigFake:
    """Config whose Telegram token path explodes if accidentally inspected."""

    @property
    def telegram_bot_token_file(self) -> str:
        raise AssertionError("the companion must not read Hermes' Telegram token")


class HermesHelperFake:
    def __init__(self) -> None:
        self.mode = "sent"
        self.calls: list[dict[str, object]] = []
        self.next_message_id = 100

    def send_notification(
        self, *, chat_id: int, text: str, parse_mode: str = "HTML"
    ) -> object:
        self.calls.append({"chat_id": chat_id, "text": text, "parse_mode": parse_mode})
        if self.mode == "retryable-pretransmission":
            return {"status": self.mode, "retry_after": 0}
        if self.mode == "ambiguous":
            return {"status": self.mode, "transmitted": True}
        if self.mode == "permanent":
            return {"status": self.mode, "transmitted": False}
        self.next_message_id += 1
        return {"status": "sent", "message_id": self.next_message_id}


class PolicyFake:
    def __init__(self, helper: HermesHelperFake) -> None:
        self.helper = helper
        self.include_admin = True
        self.mutations: list[tuple[str, int, str]] = []
        self.fingerprint = "policy-fingerprint"
        self.version = "7"

    def current_users(self) -> dict[str, object]:
        users: list[dict[str, object]] = []
        if self.include_admin:
            users.append({"user_id": 1, "role": "admin"})
        users.append({"user_id": 7, "role": "user"})
        return {
            "users": users,
            "fingerprint": self.fingerprint,
            "version": self.version,
        }

    def resolve_identity(self, *, user_id: int) -> dict[str, object]:
        return {"user_id": user_id, "chat_id": -100, "allowed": True, "role": "admin"}

    def authorize(
        self, *, user_id: int, chat_id: int, require_admin: bool = False
    ) -> dict[str, object]:
        return {
            "user_id": user_id,
            "chat_id": chat_id,
            "allowed": True,
            "role": "admin" if require_admin or user_id == 1 else "user",
            "fingerprint": self.fingerprint,
            "version": self.version,
        }

    def add_user(self, *, user_id: int, expected_fingerprint: str) -> dict[str, object]:
        self.mutations.append(("add", user_id, expected_fingerprint))
        return {"ok": True, "changed": True, "user_id": user_id}

    def remove_user(
        self, *, user_id: int, expected_fingerprint: str
    ) -> dict[str, object]:
        self.mutations.append(("remove", user_id, expected_fingerprint))
        return {"ok": True, "changed": True, "user_id": user_id}


class UpstreamFake:
    registered_tools = UPSTREAM_TOOL_SET

    def call_tool(self, name: str, arguments: object | None = None) -> object:
        return {"tool": name, "arguments": arguments or {}}


class ArrFake:
    def __init__(self) -> None:
        self.movie_calls = 0
        self.series_calls = 0
        self.added_movies: list[int] = []
        self.added_series: list[int] = []

    def search_movie(self, query: str, *, limit: int = 100) -> object:
        del query, limit
        return SimpleNamespace(
            items=(MediaCandidate(MediaType.MOVIE, 10, "Fixture Movie", 2026),)
        )

    def search_series(self, query: str, *, limit: int = 100) -> object:
        del query, limit
        return SimpleNamespace(
            items=(MediaCandidate(MediaType.SERIES, 20, "Fixture Series", 2026),)
        )

    def find_existing_movie(self, tmdb_id: int) -> object | None:
        del tmdb_id
        return None

    def lookup_movie(self, tmdb_id: int, **_: object) -> list[dict[str, object]]:
        return [{"id": 501, "tmdbId": tmdb_id, "title": "Fixture Movie"}]

    def add_movie(self, movie: object, **_: object) -> dict[str, object]:
        self.movie_calls += 1
        provider_id = int(movie["tmdbId"]) if isinstance(movie, dict) else 10
        self.added_movies.append(provider_id)
        return {"id": 501, "tmdbId": provider_id, "title": "Fixture Movie"}

    def find_existing_series(self, tvdb_id: int) -> object | None:
        del tvdb_id
        return {
            "id": 601,
            "tvdbId": 20,
            "title": "Fixture Series",
            "seasons": [{"seasonNumber": 1}],
        }

    def lookup_series(self, tvdb_id: int, **_: object) -> list[dict[str, object]]:
        return [{"id": 601, "tvdbId": tvdb_id, "title": "Fixture Series"}]

    def add_series(self, series: object, **_: object) -> dict[str, object]:
        self.series_calls += 1
        provider_id = int(series["tvdbId"]) if isinstance(series, dict) else 20
        self.added_series.append(provider_id)
        return {"id": 601, "tvdbId": provider_id, "title": "Fixture Series"}

    def update_series(self, series: object, **_: object) -> object:
        return series

    def search_season(self, series_id: int, season_number: int) -> dict[str, object]:
        return {"id": series_id * 10 + season_number}

    def get_series(self, series_id: int) -> dict[str, object]:
        return {"id": series_id, "status": "ended"}

    def list_episode_records(
        self, series_id: int, *, season_number: int
    ) -> list[dict[str, object]]:
        return [
            {
                "id": series_id * 100 + season_number,
                "seasonNumber": season_number,
                "episodeNumber": 1,
                "title": "Episode 1",
                "hasFile": False,
                "airDate": "2026-01-01",
                "status": "released",
            }
        ]


class PlexFake:
    server_uuid = "srv-1"
    allowed_library_keys: tuple[str, ...] = ()
    allowed_library_names = ("Movies",)

    def __init__(self) -> None:
        self.library = SimpleNamespace(uuid="lib-1", key="Movies", title="Movies")
        self.items: list[PlexItem] = []
        self.metadata: dict[str, object] = {}
        self.search_items: list[MediaCandidate] = []

    def libraries(self) -> list[object]:
        return [self.library]

    def iter_library_items(self, library: object, **_: object) -> list[PlexItem]:
        assert library is self.library
        return list(self.items)

    def get_metadata(self, rating_key: str) -> object:
        return self.metadata[rating_key]

    def search(self, query: str, **_: object) -> object:
        del query
        return SimpleNamespace(items=tuple(self.search_items))

    def status_for_identity(self, identity: MediaIdentity) -> object:
        return SimpleNamespace(available=False, identity=identity)


@dataclass
class Bundle:
    database: Database
    runtime: Any
    plex: PlexFake
    arr: ArrFake
    policy: PolicyFake
    helper: HermesHelperFake
    claims: SimpleNamespace


def make_bundle(tmp_path: Path) -> Bundle:
    database = Database(tmp_path / "production.sqlite3")
    database.migrate()
    helper = HermesHelperFake()
    policy = PolicyFake(helper)
    plex = PlexFake()
    arr = ArrFake()
    nonce = DurableNonceFake(database)
    verifier = ActorAssertionVerifier(keys={"current": b"a" * 32}, nonce_store=nonce)
    confirmation = DurableConfirmationFake(database)
    runtime = build_runtime(
        config=ConfigFake(),
        database=database,
        rate_limiter=SQLiteRateLimiter(database),
        nonce_store=nonce,
        confirmation_store=confirmation,
        actor_verifier=verifier,
        policy=policy,
        upstream=UpstreamFake(),
        dashboard_api_key=b"b" * 32,
        helper_key=b"c" * 32,
        plex_capability="p" * 43,
        expected_server_uuid="srv-1",
        allowed_server_uuids=("srv-1",),
        allowed_library_names=("Movies",),
        trusted_ingress_peers=("127.0.0.1",),
        plex=plex,
        radarr=arr,
        sonarr=arr,
        notification_helper=helper,
    )
    claims = SimpleNamespace(
        user_id=7,
        chat_id=8,
        update_id=100,
        chat_type="private",
        update_type="message",
        allowlist_fingerprint=None,
    )
    return Bundle(database, runtime, plex, arr, policy, helper, claims)


def event(item: PlexItem, *, source: str = "plex_webhook") -> NormalizedPlexEvent:
    return NormalizedPlexEvent(
        event_type="library.new",
        server_uuid="srv-1",
        machine_identifier="machine-1",
        library_uuid="lib-1",
        library_name="Movies",
        rating_key=item.rating_key,
        media_type=item.media_type.value,
        title=item.title,
        year=item.year,
        season_number=item.season_number,
        episode_number=item.episode_number,
        added_at=item.added_at,
        source=source,
        observed_at=NOW,
    )


def metadata(item: PlexItem, *, playable: bool, verified: bool = True) -> object:
    return SimpleNamespace(
        item=item,
        snapshot_verified=verified,
        playable=playable,
        snapshot=SimpleNamespace(playable=playable),
        provider_identity=item.provider_identity,
    )


def movie_item(
    rating_key: str = "10",
    *,
    added_at: datetime | None = NOW,
    provider_id: int = 10,
    title: str = "Fixture Movie",
) -> PlexItem:
    return PlexItem(
        rating_key=rating_key,
        media_type=MediaType.MOVIE,
        title=title,
        year=2026,
        library_key="Movies",
        library_name="Movies",
        quality="1080p",
        plex_url=f"https://app.plex.tv/desktop#!/server/machine-1/details?key=/library/metadata/{rating_key}",
        added_at=added_at,
        machine_identifier="machine-1",
        provider_identity=MediaIdentity(
            MediaType.MOVIE, tmdb_id=provider_id, provider_id=str(provider_id)
        ),
    )


def activate_for_test(
    bundle: Bundle, *, activated_at: str = "2026-08-15T11:00:00.000Z"
) -> None:
    with bundle.database.transaction() as connection:
        connection.execute(
            "UPDATE activation SET status='active', delivery_enabled=1, activated_at=? WHERE activation_id='media-companion'",
            (activated_at,),
        )
        connection.execute(
            "INSERT OR REPLACE INTO activation_cursors(activation_id,server_uuid,library_uuid,scan_generation,last_incremental_at,last_full_sweep_at,status) VALUES ('media-companion','srv-1','lib-1',1,?,?, 'complete')",
            (activated_at, activated_at),
        )


def seed_movie_subscription(
    bundle: Bundle, *, user_id: int = 7, chat_id: int = 8, provider_id: int = 10
) -> int:
    with bundle.database.transaction() as connection:
        subscription = connection.execute(
            "INSERT INTO subscriptions(user_id,chat_id,destination,notification_class,media_type,provider_id,tmdb_id,mode,generation,status) VALUES (?, ?, ?, 'requester', 'movie', ?, ?, 'movie', 1, 'active')",
            (user_id, chat_id, str(chat_id), str(provider_id), provider_id),
        ).lastrowid
        assert subscription is not None
        unit = connection.execute(
            "INSERT INTO subscription_units(subscription_id,logical_unit_key,unit_type,provider_id,status) VALUES (?, ?, 'movie', ?, 'tracking')",
            (subscription, f"movie:{provider_id}", str(provider_id)),
        ).lastrowid
        assert unit is not None
        return int(unit)


def seed_episode_units(
    bundle: Bundle, *, count: int = 2, mode: str = "season_completion", chat_id: int = 8
) -> tuple[int, ...]:
    with bundle.database.transaction() as connection:
        subscription = connection.execute(
            "INSERT INTO subscriptions(user_id,chat_id,destination,notification_class,media_type,provider_id,tvdb_id,season_number,mode,generation,status) VALUES (7, ?, ?, 'requester', 'series', '20', 20, 1, ?, 1, 'active')",
            (chat_id, str(chat_id), mode),
        ).lastrowid
        assert subscription is not None
        ids: list[int] = []
        for number in range(1, count + 1):
            item = movie_item(
                str(100 + number),
                provider_id=20,
                title=f"Episode {number}",
                added_at=NOW + timedelta(seconds=number),
            )
            # Rebuild as an episode because PlexItem validates episode scope.
            item = PlexItem(
                rating_key=item.rating_key,
                media_type=MediaType.EPISODE,
                title=item.title,
                year=2026,
                library_key="Movies",
                library_name="Movies",
                show_title="Fixture Series",
                season_number=1,
                episode_number=number,
                plex_url=item.plex_url,
                added_at=item.added_at,
                machine_identifier="machine-1",
                provider_identity=MediaIdentity(
                    MediaType.EPISODE, tvdb_id=20, provider_id="20"
                ),
            )
            connection.execute(
                "INSERT INTO plex_items(server_uuid,library_uuid,machine_identifier,rating_key,tombstone_generation,media_type,title,year,show_title,season_number,episode_number,library_key,library_name,tvdb_id,plex_url,added_at,visible_in_plex_at,lifecycle_status) VALUES ('srv-1','lib-1',?,?,?,?,? ,?,?,?,?,?,?,20,?,?,?,'active')",
                (
                    item.machine_identifier,
                    item.rating_key,
                    0,
                    item.media_type.value,
                    item.title,
                    item.year,
                    item.show_title,
                    item.season_number,
                    item.episode_number,
                    item.library_key,
                    item.library_name,
                    item.plex_url,
                    NOW.isoformat().replace("+00:00", "Z"),
                    (NOW + timedelta(seconds=number))
                    .isoformat()
                    .replace("+00:00", "Z"),
                ),
            )
            plex_id = int(
                connection.execute("SELECT last_insert_rowid()").fetchone()[0]
            )
            unit = connection.execute(
                "INSERT INTO subscription_units(subscription_id,logical_unit_key,unit_type,provider_id,season_number,episode_number,status,visible_in_plex_at,plex_item_id) VALUES (?, ?, 'episode', '20', 1, ?, 'available', ?, ?)",
                (
                    subscription,
                    f"series:20:season:1:episode:{number}",
                    number,
                    (NOW + timedelta(seconds=number))
                    .isoformat()
                    .replace("+00:00", "Z"),
                    plex_id,
                ),
            ).lastrowid
            assert unit is not None
            ids.append(int(unit))
    return tuple(ids)


def test_build_runtime_uses_hermes_notification_seam_without_duplicate_token(
    tmp_path: Path,
) -> None:
    bundle = make_bundle(tmp_path)
    assert isinstance(bundle.runtime.operations.telegram, HermesTelegramBridge)
    assert bundle.runtime.production is True
    assert bundle.runtime.worker is not None
    assert bundle.runtime.worker._activation_status() == "pending"


def test_private_confirmation_executes_only_the_durable_bound_arguments(
    tmp_path: Path,
) -> None:
    bundle = make_bundle(tmp_path)
    executor = bundle.runtime.confirmation_executor
    assert callable(executor)
    token = "A" * 43
    arguments = {"user_id": 42, "expected_fingerprint": bundle.policy.fingerprint}
    expires_at = int(datetime.now(UTC).timestamp()) + 300
    token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    argument_store = bundle.runtime.confirmation_arguments_store
    assert argument_store is not None
    argument_store.put(
        token_hash=token_hash,
        tool="users_add",
        argument_hash=canonical_argument_hash(arguments),
        arguments=arguments,
        expires_at=expires_at,
    )
    record = ConfirmationRecord(
        token_hash=token_hash,
        actor_user_id=1,
        actor_chat_id=-100,
        tool="users_add",
        argument_hash=canonical_argument_hash(arguments),
        target_identity="users:42",
        state_fingerprint="target-state",
        preview_hash="preview-hash",
        policy_version=bundle.policy.version,
        nonce="confirmation-nonce",
        issued_at=expires_at - 300,
        expires_at=expires_at,
        state="consumed",
    )

    result = executor(
        record, arguments={"user_id": 99, "expected_fingerprint": "stale"}
    )
    assert result["changed"] is True
    assert bundle.policy.mutations[-1] == ("add", 42, bundle.policy.fingerprint)
    with bundle.database.connection() as connection:
        consumed = connection.execute(
            "SELECT consumed_at FROM production_confirmation_arguments WHERE token_hash=?",
            (record.token_hash,),
        ).fetchone()
    assert consumed is not None and consumed[0]


def test_search_issues_actor_update_bound_handle_and_requests_resolve_only_that_handle(
    tmp_path: Path,
) -> None:
    bundle = make_bundle(tmp_path)
    operations = bundle.runtime.operations
    page = operations.search_media(
        {"query": "Fixture", "media_type": "movie", "limit": 25},
        claims=bundle.claims,
    )
    candidate = page.items[0]
    handle = candidate.candidate_handle
    assert isinstance(handle, str) and len(handle) >= 20

    result = operations.request_movie(
        {"candidate_handle": handle, "idempotency_key": "movie-1"},
        claims=bundle.claims,
    )
    assert result.intent.provider_id == 10
    assert result.intent.status.value in {"accepted", "requested"}

    series_page = operations.search_media(
        {"query": "Fixture", "media_type": "series", "limit": 25},
        claims=SimpleNamespace(**{**vars(bundle.claims), "update_id": 101}),
    )
    series_handle = series_page.items[0].candidate_handle
    assert isinstance(series_handle, str) and len(series_handle) >= 20
    series_result = operations.request_series(
        {
            "candidate_handle": series_handle,
            "seasons": [1],
            "idempotency_key": "series-1",
        },
        claims=SimpleNamespace(**{**vars(bundle.claims), "update_id": 101}),
    )
    assert series_result.intent.provider_id == 20

    with pytest.raises(ConflictError):
        operations.request_movie(
            {
                "tmdb_id": 10,
                "candidate_handle": "A" * 43,
                "idempotency_key": "invented",
            },
            claims=bundle.claims,
        )
    with pytest.raises(ConflictError):
        operations.request_movie(
            {
                "tmdb_id": 10,
                "candidate_handle": handle,
                "idempotency_key": "cross-actor",
            },
            claims=SimpleNamespace(**{**vars(bundle.claims), "user_id": 99}),
        )


def test_activation_needs_two_complete_passes_and_only_pass_two_new_is_obligation(
    tmp_path: Path,
) -> None:
    bundle = make_bundle(tmp_path)
    old = movie_item("10", added_at=NOW)
    bundle.plex.items = [old]
    bundle.plex.metadata[old.rating_key] = metadata(old, playable=True)
    worker = bundle.runtime.worker
    assert worker is not None

    worker.run_cycle(force_full=True)
    with bundle.database.connection() as connection:
        first = connection.execute(
            "SELECT status,delivery_enabled,baseline_started_at FROM activation WHERE activation_id='media-companion'"
        ).fetchone()
    assert first is not None
    assert (str(first[0]), int(first[1])) != ("active", 1)
    baseline_started = datetime.fromisoformat(str(first[2]).replace("Z", "+00:00"))

    new = movie_item(
        "11",
        added_at=baseline_started + timedelta(seconds=1),
        provider_id=11,
        title="New Movie",
    )
    bundle.plex.items = [old, new]
    bundle.plex.metadata[new.rating_key] = metadata(new, playable=True)
    worker.run_cycle(force_full=True)
    with bundle.database.connection() as connection:
        activation = connection.execute(
            "SELECT status,delivery_enabled FROM activation WHERE activation_id='media-companion'"
        ).fetchone()
        members = connection.execute(
            "SELECT rating_key,pass_number,classification FROM activation_members ORDER BY rating_key"
        ).fetchall()
        admin_payloads = connection.execute(
            "SELECT g.payload_json FROM deliveries d JOIN notification_groups g ON g.id=d.group_id WHERE d.notification_class='admin'"
        ).fetchall()
    assert activation is not None and (str(activation[0]), int(activation[1])) == (
        "active",
        1,
    )
    assert any(
        str(row[0]) == "11" and int(row[1]) == 2 and str(row[2]) == "new"
        for row in members
    )
    assert all("Fixture Movie" not in str(row[0]) for row in admin_payloads)
    assert any("New Movie" in str(row[0]) for row in admin_payloads)


def test_movie_notice_requires_snapshot_verified_and_playable_evidence(
    tmp_path: Path,
) -> None:
    bundle = make_bundle(tmp_path)
    bundle.policy.include_admin = False
    activate_for_test(bundle)
    unit_id = seed_movie_subscription(bundle)
    worker = bundle.runtime.worker
    assert worker is not None
    worker.leader = bundle.database.acquire_leader(worker.worker_id, lease_name="media")
    assert worker.leader is not None

    item = movie_item("10")
    bundle.plex.metadata[item.rating_key] = metadata(
        item, playable=False, verified=False
    )
    with pytest.raises(ProviderRetryable):
        worker.process_event(event(item))
    with bundle.database.connection() as connection:
        row = connection.execute(
            "SELECT status,visible_in_plex_at FROM subscription_units WHERE id=?",
            (unit_id,),
        ).fetchone()
        assert row is not None
        assert (str(row[0]), row[1]) == ("tracking", None)
        assert connection.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 0

    bundle.plex.metadata[item.rating_key] = metadata(item, playable=True, verified=True)
    worker.process_event(event(item))
    assert worker.plan_notifications() == 1
    with bundle.database.connection() as connection:
        row = connection.execute(
            "SELECT status,visible_in_plex_at FROM subscription_units WHERE id=?",
            (unit_id,),
        ).fetchone()
        assert row is not None and str(row[0]) == "available" and row[1]
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM deliveries WHERE notification_class='requester'"
            ).fetchone()[0]
            == 1
        )


def test_planning_is_idempotent_and_bulk_season_uses_one_group(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    bundle.policy.include_admin = False
    activate_for_test(bundle)
    seed_episode_units(bundle, count=2, mode="season_completion")
    worker = bundle.runtime.worker
    assert worker is not None
    assert worker.plan_notifications() == 1
    assert worker.plan_notifications() == 0
    with bundle.database.connection() as connection:
        groups = connection.execute(
            "SELECT COUNT(*) FROM notification_groups"
        ).fetchone()[0]
        deliveries = connection.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
    assert groups == 1
    assert deliveries == 1


def test_same_chat_admin_and_requester_are_one_merged_obligation(
    tmp_path: Path,
) -> None:
    bundle = make_bundle(tmp_path)
    activate_for_test(bundle)
    seed_movie_subscription(bundle, chat_id=-100)
    worker = bundle.runtime.worker
    assert worker is not None
    item = movie_item("10")
    bundle.plex.metadata[item.rating_key] = metadata(item, playable=True)
    worker.leader = bundle.database.acquire_leader(worker.worker_id, lease_name="media")
    assert worker.leader is not None
    worker.process_event(event(item))
    assert worker.plan_notifications() == 1
    with bundle.database.connection() as connection:
        groups = connection.execute(
            "SELECT notification_class,chat_id FROM notification_groups"
        ).fetchall()
        deliveries = connection.execute(
            "SELECT notification_class,chat_id FROM deliveries"
        ).fetchall()
    assert (
        len(groups) == 1 and str(groups[0][0]) == "admin" and int(groups[0][1]) == -100
    )
    assert len(deliveries) == 1 and str(deliveries[0][0]) == "admin"


@pytest.mark.parametrize(
    ("mode", "expected"),
    (
        ("sent", "sent"),
        ("retryable-pretransmission", "retry_wait"),
        ("ambiguous", "unknown"),
        ("permanent", "failed"),
    ),
)
def test_delivery_due_boundary_and_pretransmission_ambiguous_permanent_outcomes(
    tmp_path: Path, mode: str, expected: str
) -> None:
    bundle = make_bundle(tmp_path)
    bundle.policy.include_admin = False
    activate_for_test(bundle)
    seed_movie_subscription(bundle)
    item = movie_item("10")
    worker = bundle.runtime.worker
    assert worker is not None
    worker.leader = bundle.database.acquire_leader(worker.worker_id, lease_name="media")
    assert worker.leader is not None
    bundle.plex.metadata[item.rating_key] = metadata(item, playable=True)
    worker.process_event(event(item))
    assert worker.plan_notifications() == 1
    with bundle.database.connection() as connection:
        delivery_id = int(connection.execute("SELECT id FROM deliveries").fetchone()[0])
        connection.execute(
            "UPDATE notification_groups SET due_at=?", ("2099-01-01T00:00:00Z",)
        )
    assert worker.deliver_pending() == 0
    assert not bundle.helper.calls
    with bundle.database.transaction() as connection:
        connection.execute(
            "UPDATE notification_groups SET due_at=?", ("2000-01-01T00:00:00Z",)
        )
    bundle.helper.mode = mode
    assert worker.deliver_pending() == (1 if expected == "sent" else 0)
    with bundle.database.connection() as connection:
        assert (
            str(
                connection.execute(
                    "SELECT status FROM deliveries WHERE id=?", (delivery_id,)
                ).fetchone()[0]
            )
            == expected
        )


def test_restart_duplicate_event_and_tombstone_readd_keep_one_generation(
    tmp_path: Path,
) -> None:
    bundle = make_bundle(tmp_path)
    activate_for_test(bundle)
    worker = bundle.runtime.worker
    assert worker is not None
    worker.leader = bundle.database.acquire_leader(worker.worker_id, lease_name="media")
    assert worker.leader is not None
    old = movie_item("10", added_at=NOW)
    bundle.plex.metadata[old.rating_key] = metadata(old, playable=True)
    inbox = bundle.runtime.event_inbox
    first = inbox.persist_event(event(old))
    duplicate = inbox.persist_event(event(old))
    assert first["id"] == duplicate["id"] and duplicate["duplicate"] is True
    worker.drain_inbox(worker.leader)
    with bundle.database.transaction() as connection:
        connection.execute(
            "UPDATE plex_items SET lifecycle_status='tombstone' WHERE rating_key='10'"
        )
    readded = movie_item("10", added_at=NOW + timedelta(days=1))
    bundle.plex.metadata[readded.rating_key] = metadata(readded, playable=True)
    worker.process_event(event(readded, source="plex_reconciliation"))
    with bundle.database.connection() as connection:
        generations = connection.execute(
            "SELECT tombstone_generation,lifecycle_status FROM plex_items WHERE rating_key='10' ORDER BY tombstone_generation"
        ).fetchall()
    assert [(int(row[0]), str(row[1])) for row in generations] == [
        (0, "tombstone"),
        (1, "active"),
    ]


def test_dashboard_service_principal_confirmation_executes_exact_bound_mutation(
    tmp_path: Path,
) -> None:
    bundle = make_bundle(tmp_path)
    runtime = bundle.runtime
    identity = runtime.dashboard_identity_resolver("dashboard-admin")
    assert identity["actor"] == "dashboard-admin"
    arguments = {
        "user_id": 42,
        "fingerprint": bundle.policy.fingerprint,
        "idempotency_key": "add-42",
        "version": 7,
    }
    preview = "Confirm users.add user_id=42"
    digest = hashlib.sha256(preview.encode()).hexdigest()
    issued = runtime.dashboard_confirmation_issuer(
        actor="dashboard-admin",
        identity=identity,
        operation="users.add",
        arguments=arguments,
        preview=preview,
        preview_digest=digest,
    )
    capability = issued["confirmation_capability"]
    assert runtime.dashboard_confirmation_guard(
        actor="dashboard-admin",
        identity=identity,
        operation="users.add",
        arguments=arguments,
        preview=preview,
        preview_digest=digest,
        confirmation=capability,
    )
    result = runtime.dashboard_handlers["users.add"](arguments=arguments)
    assert result["changed"] is True
    assert bundle.policy.mutations[-1] == ("add", 42, bundle.policy.fingerprint)
