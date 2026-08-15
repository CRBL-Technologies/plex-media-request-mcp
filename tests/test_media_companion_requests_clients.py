from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from collections.abc import Mapping
import hashlib
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from media_companion.clients.plex import PlexClient
from media_companion.clients.radarr import (
    AdapterConfigurationError,
    AdapterResponseError,
    AdapterTransportError,
    HTTPResponse,
    RadarrClient,
    RadarrDefaults,
    _ConfiguredHTTPTransport,
)
from media_companion.clients.radarr import FileSecretReader
from media_companion.clients.sonarr import SonarrClient
from media_companion.clients.telegram import (
    NotificationLine,
    TelegramError,
    TelegramErrorClass,
    TelegramClient,
    classify_telegram_error,
    render_notification,
)
from media_companion.db import Database
from media_companion.config import SecretFileRef
from media_companion.errors import ConflictError
from media_companion.models import (
    MediaCandidate,
    MediaIdentity,
    MediaType,
    RequestMode,
    RequestStatus,
)
from media_companion.requests import RequestActor, RequestWorkflow, SQLiteRequestStore
from media_companion.safe_views import serialize_record


class FakeTransport:
    def __init__(self, responses: list[HTTPResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def request(self, method: str, url: str, **kwargs: object) -> HTTPResponse:
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected provider request")
        return self.responses.pop(0)


class AdapterTests(unittest.TestCase):
    @staticmethod
    def _payload(call: tuple[str, str, dict[str, object]]) -> Mapping[str, object]:
        value = call[2].get("json_body")
        if not isinstance(value, Mapping):
            raise AssertionError("request payload is not a mapping")
        return value

    def test_shared_env_file_uses_the_exact_selected_credential(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "upstream.env"
            path.write_text(
                "RADARR_API_KEY=radarr-secret\nSONARR_API_KEY=sonarr-secret\n",
                encoding="utf-8",
            )
            reader = FileSecretReader()

            self.assertEqual(
                reader.read_secret(SecretFileRef(path, key="RADARR_API_KEY")),
                "radarr-secret",
            )
            self.assertEqual(
                reader.read_secret(SecretFileRef(path, key="SONARR_API_KEY")),
                "sonarr-secret",
            )
            with self.assertRaises(ValueError):
                reader.read_secret(SecretFileRef(path))

    def test_search_candidate_wire_form_has_handle_but_no_provider_id(self) -> None:
        candidate = MediaCandidate(
            MediaType.MOVIE,
            10,
            "Movie",
            candidate_handle="A" * 32,
        )
        wire = serialize_record(candidate)
        self.assertEqual(wire["candidate_handle"], "A" * 32)
        self.assertNotIn("provider_id", wire)
        self.assertNotIn("tmdbId", wire)
        self.assertNotIn("tvdbId", wire)

    def test_radarr_add_keeps_defaults_server_owned(self) -> None:
        transport = FakeTransport(
            [HTTPResponse(200, {}, b'{"id": 5, "tmdbId": 10, "title": "Movie"}')]
        )
        client = RadarrClient(
            "http://radarr:7878/",
            "radarr-secret",
            transport=transport,
            defaults=RadarrDefaults(
                quality_profile_id=7, root_folder_path="/movies", tag_ids=(3,)
            ),
        )
        result = client.add_movie({"tmdbId": 10, "title": "Movie"})
        self.assertEqual(result.id, 5)
        payload = self._payload(transport.calls[0])
        self.assertEqual(payload["qualityProfileId"], 7)
        self.assertEqual(payload["rootFolderPath"], "/movies")
        self.assertEqual(payload["tags"], [3])
        self.assertNotIn("radarr-secret", repr(client))

    def test_sonarr_season_search_is_one_command_per_season(self) -> None:
        transport = FakeTransport(
            [
                HTTPResponse(200, {}, b'{"id": 11}'),
                HTTPResponse(200, {}, b'{"id": 12}'),
                HTTPResponse(200, {}, b'{"id": 13}'),
            ]
        )
        client = SonarrClient(
            "http://sonarr:8989", "sonarr-secret", transport=transport
        )
        client.search_seasons(44, [2, 1, 2])
        self.assertEqual(
            [self._payload(call)["seasonNumber"] for call in transport.calls], [1, 2]
        )
        self.assertTrue(
            all(
                self._payload(call)["name"] == "SeasonSearch"
                for call in transport.calls
            )
        )

    def test_plex_rating_key_is_constructed_and_token_stays_out_of_link(self) -> None:
        transport = FakeTransport(
            [
                HTTPResponse(
                    200,
                    {},
                    b'{"MediaContainer":{"Metadata":[{"ratingKey":"12","type":"movie","title":"Movie","libraryKey":"1","libraryName":"Movies","Guid":[{"id":"tmdb://10"}],"Media":[{"Part":[{"file":"/library/movies/movie.mkv"}]}]}]}}',
                )
            ]
        )
        client = PlexClient(
            "http://plex:32400",
            "plex-secret",
            machine_identifier="machine",
            server_uuid="server",
            allowed_library_keys=("1",),
            transport=transport,
        )
        item = client.get_metadata("12").item
        self.assertEqual(transport.calls[0][1], "http://plex:32400/library/metadata/12")
        self.assertNotIn("plex-secret", item.plex_url or "")
        with self.assertRaises(ValueError):
            client.get_metadata("12/../../etc")

    def test_stable_provider_ids_are_required_for_search_and_add_responses(
        self,
    ) -> None:
        radarr_transport = FakeTransport(
            [HTTPResponse(200, {}, b'[{"id": 5, "title": "No stable id"}]')]
        )
        radarr = RadarrClient(
            "http://radarr:7878", "radarr-secret", transport=radarr_transport
        )
        self.assertEqual(radarr.search_movie("No stable id").items, ())

        sonarr_transport = FakeTransport(
            [HTTPResponse(200, {}, b'[{"id": 8, "title": "No stable id"}]')]
        )
        sonarr = SonarrClient(
            "http://sonarr:8989", "sonarr-secret", transport=sonarr_transport
        )
        self.assertEqual(sonarr.search_series("No stable id").items, ())

        mismatch_transport = FakeTransport(
            [HTTPResponse(200, {}, b'{"id": 5, "tmdbId": 11, "title": "Wrong"}')]
        )
        mismatch = RadarrClient(
            "http://radarr:7878", "radarr-secret", transport=mismatch_transport
        )
        with self.assertRaises(AdapterResponseError):
            mismatch.add_movie({"tmdbId": 10, "title": "Movie"})

    def test_sonarr_update_preserves_existing_server_state(self) -> None:
        transport = FakeTransport(
            [
                HTTPResponse(
                    200,
                    {},
                    b'{"id": 4, "tvdbId": 22, "title": "Series", "monitored": true, "qualityProfileId": 7, "rootFolderPath": "/shows", "seasonFolder": false, "tags": [3]}',
                )
            ]
        )
        client = SonarrClient(
            "http://sonarr:8989", "sonarr-secret", transport=transport
        )
        client.update_series(
            {
                "id": 4,
                "tvdbId": 22,
                "title": "Series",
                "monitored": True,
                "qualityProfileId": 7,
                "rootFolderPath": "/shows",
                "seasonFolder": False,
                "tags": [3],
                "seasons": [
                    {"seasonNumber": 0, "monitored": False},
                    {"seasonNumber": 1, "monitored": False},
                ],
            },
            seasons=[1],
        )
        payload = self._payload(transport.calls[0])
        self.assertEqual(payload["monitored"], True)
        self.assertEqual(payload["qualityProfileId"], 7)
        self.assertEqual(payload["rootFolderPath"], "/shows")
        self.assertEqual(payload["seasonFolder"], False)
        self.assertEqual(payload["tags"], [3])
        seasons = payload["seasons"]
        assert isinstance(seasons, list)
        self.assertEqual(
            [
                (row["seasonNumber"], row["monitored"])
                for row in seasons
                if isinstance(row, Mapping)
            ],
            [(0, False), (1, True)],
        )

    def test_plex_scope_identity_and_poster_are_fail_closed(self) -> None:
        with self.assertRaises(AdapterConfigurationError):
            PlexClient(
                "http://plex:32400", "plex-secret", transport=FakeTransport([])
            ).get_metadata("1")

        conflict = PlexClient(
            "http://plex:32400",
            "plex-secret",
            server_uuid="server",
            allowed_library_keys=("1",),
            transport=FakeTransport(
                [
                    HTTPResponse(
                        200,
                        {},
                        b'{"MediaContainer":{"Metadata":[{"ratingKey":"1","type":"movie","title":"Conflict","libraryKey":"1","Guid":[{"id":"tmdb://10"}],"tmdbId":11}]}}',
                    )
                ]
            ),
        )
        with self.assertRaises(AdapterResponseError):
            conflict.get_metadata("1")

        poster = PlexClient(
            "http://plex:32400",
            "plex-secret",
            server_uuid="server",
            allowed_library_keys=("1",),
            transport=FakeTransport(
                [HTTPResponse(200, {"content-type": "image/jpeg"}, b"not-an-image")]
            ),
        )
        with self.assertRaises(AdapterResponseError):
            poster.poster_bytes("1")

    def test_plex_status_never_promotes_title_only_hub_to_visibility(self) -> None:
        transport = FakeTransport(
            [
                HTTPResponse(
                    200,
                    {},
                    b'{"MediaContainer":{"Hub":[{"type":"show","Metadata":[{"ratingKey":"1","type":"show","title":"Series","libraryKey":"1","Guid":[{"id":"tvdb://22"}]}]}]}}',
                ),
            ]
        )
        client = PlexClient(
            "http://plex:32400",
            "plex-secret",
            server_uuid="server",
            allowed_library_keys=("1",),
            transport=transport,
        )
        status = client.status_for_identity(
            # A provider identity is stable, but the hub response is not
            # playable evidence until its metadata is re-fetched.
            MediaIdentity(MediaType.SERIES, tvdb_id=22),
        )
        self.assertFalse(status.available)

    def test_configured_transport_rejects_origin_change_and_dns_rebinding(self) -> None:
        class Session:
            trust_env = True
            proxies: dict[str, str] = {}

            def request(self, *_args: object, **_kwargs: object) -> object:
                raise AssertionError("the fake session should not be reached")

        transport = _ConfiguredHTTPTransport(
            session=cast(Any, Session()), allowed_origin="https://provider.example"
        )
        with self.assertRaises(AdapterConfigurationError):
            transport.request("GET", "https://other.example/path")
        with patch(
            "media_companion.clients.radarr.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("93.184.216.10", 443))],
        ):
            with self.assertRaises(AssertionError):
                transport.request("GET", "https://provider.example/path")
        with patch(
            "media_companion.clients.radarr.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("93.184.216.11", 443))],
        ):
            with self.assertRaises(AdapterTransportError):
                transport.request("GET", "https://provider.example/path")


class TelegramTests(unittest.TestCase):
    def test_renderer_is_escaped_and_deterministic(self) -> None:
        unit = NotificationLine(
            "<Movie &>",
            year=2026,
            season_number=1,
            episode_number=2,
            plex_url="https://app.plex.tv/desktop/x",
        )
        first = render_notification([unit])
        self.assertEqual(first, render_notification([unit]))
        self.assertIn("&lt;Movie &amp;&gt;", first[0])
        self.assertIn("Open in Plex", first[0])

    def test_error_classification_distinguishes_recipient_and_ambiguous(self) -> None:
        self.assertEqual(
            classify_telegram_error(403, "Forbidden: bot was blocked by the user"),
            TelegramErrorClass.TERMINAL_RECIPIENT,
        )
        self.assertEqual(
            classify_telegram_error(500, "Internal Server Error"),
            TelegramErrorClass.AMBIGUOUS,
        )
        self.assertEqual(
            classify_telegram_error(429, "Too Many Requests", retry_after=3),
            TelegramErrorClass.RATE_LIMITED,
        )

    def test_send_message_raises_safe_typed_error(self) -> None:
        transport = FakeTransport(
            [
                HTTPResponse(
                    403,
                    {},
                    b'{"ok":false,"error_code":403,"description":"Forbidden: bot was blocked by the user"}',
                )
            ]
        )
        client = TelegramClient("telegram-secret", transport=transport)
        with self.assertRaises(TelegramError) as raised:
            client.send_message(123, "hello")
        self.assertEqual(
            raised.exception.error_class, TelegramErrorClass.TERMINAL_RECIPIENT
        )
        self.assertNotIn("telegram-secret", str(raised.exception))

    def test_telegram_outcomes_expose_transmission_and_migration(self) -> None:
        migrated = TelegramClient(
            "telegram-secret",
            transport=FakeTransport(
                [
                    HTTPResponse(
                        400,
                        {},
                        b'{"ok":false,"error_code":400,"description":"migrate","parameters":{"migrate_to_chat_id":-1009}}',
                    )
                ]
            ),
        )
        with self.assertRaises(TelegramError) as raised:
            migrated.send_message(123, "hello")
        self.assertTrue(raised.exception.transmitted)
        self.assertEqual(raised.exception.migrate_to_chat_id, -1009)

        class FailingTransport:
            def __init__(self, transmitted: bool) -> None:
                self.transmitted = transmitted

            def request(self, *_args: object, **_kwargs: object) -> HTTPResponse:
                raise AdapterTransportError(
                    "transport outcome", transmitted=self.transmitted
                )

        for transmitted, expected in (
            (False, TelegramErrorClass.RETRYABLE),
            (True, TelegramErrorClass.AMBIGUOUS),
        ):
            client = TelegramClient(
                "telegram-secret", transport=FailingTransport(transmitted)
            )
            with self.assertRaises(TelegramError) as failure:
                client.send_message(123, "hello")
            self.assertEqual(failure.exception.error_class, expected)
            self.assertEqual(failure.exception.pre_transmission, not transmitted)

        with self.assertRaises(AdapterConfigurationError):
            TelegramClient(
                "telegram-secret",
                endpoint="http://telegram.invalid",
                transport=FakeTransport([]),
            )
        with self.assertRaises(ValueError):
            TelegramClient("telegram-secret", transport=FakeTransport([])).send_photo(
                123, b"not-image"
            )


class RequestWorkflowTests(unittest.TestCase):
    @staticmethod
    def _movie_provider(*, lost_response: bool = False):
        class Provider:
            def __init__(self) -> None:
                self.added = False
                self.calls = 0

            def find_existing_movie(self, tmdb_id: int) -> object | None:
                return (
                    {"id": 99, "tmdbId": tmdb_id, "title": "Movie"}
                    if self.added
                    else None
                )

            def lookup_movie(
                self, tmdb_id: int, **kwargs: object
            ) -> list[dict[str, object]]:
                return [{"id": 1, "tmdbId": tmdb_id, "title": "Movie"}]

            def add_movie(self, movie: object, **kwargs: object) -> dict[str, object]:
                self.calls += 1
                self.added = True
                if lost_response:
                    raise AdapterTransportError("response lost", transmitted=True)
                return {"id": 99, "tmdbId": 10, "title": "Movie"}

        return Provider()

    def test_candidate_handle_binds_actor_update_query_and_strict_workflow(
        self,
    ) -> None:
        actor = RequestActor(7, 8, update_id=100, chat_type="private")
        with TemporaryDirectory() as directory:
            store = SQLiteRequestStore(Database(Path(directory) / "state.sqlite3"))
            provider = self._movie_provider()
            workflow = RequestWorkflow(store=store, radarr=provider)
            handle = workflow.issue_candidate(
                actor=actor,
                media_type=MediaType.MOVIE,
                provider_id=10,
                title="Movie",
                query="Movie",
            )
            self.assertGreater(len(handle), 40)
            resolved = store.resolve_candidate(handle)
            self.assertEqual(resolved.actor.update_id, 100)
            self.assertEqual(len(resolved.query_hash), 64)
            result = workflow.request_movie(
                10,
                "Movie",
                actor=actor,
                candidate_handle=handle,
                query="Movie",
                idempotency_key="update-100",
            )
            self.assertEqual(result.intent.status, RequestStatus.ACCEPTED)
            retry = workflow.request_movie(
                10,
                "Movie",
                actor=actor,
                candidate_handle=handle,
                query="Movie",
                idempotency_key="update-100",
            )
            self.assertFalse(retry.created)

            with self.assertRaises(ConflictError):
                workflow.request_movie(
                    10,
                    "Movie",
                    actor=RequestActor(7, 8, update_id=101),
                    candidate_handle=handle,
                    query="Movie",
                    idempotency_key="update-101",
                )
            with self.assertRaises(ConflictError):
                workflow.get_request(result.intent.request_id, actor=RequestActor(9, 8))
            with self.assertRaises(ConflictError):
                store.resolve_candidate("💩" * 20)

            with store.database.transaction() as connection:
                connection.execute(
                    "UPDATE request_candidates SET expires_at = '2000-01-01T00:00:00Z' WHERE handle_hash = ?",
                    (hashlib.sha256(handle.encode("ascii")).hexdigest(),),
                )
            with self.assertRaises(ConflictError):
                store.resolve_candidate(handle)

    def test_request_movie_derives_provider_id_only_from_bound_handle(self) -> None:
        actor = RequestActor(7, 8, update_id=102)
        with TemporaryDirectory() as directory:
            store = SQLiteRequestStore(Database(Path(directory) / "state.sqlite3"))
            workflow = RequestWorkflow(store=store, radarr=self._movie_provider())
            handle = workflow.issue_candidate(
                actor=actor,
                media_type=MediaType.MOVIE,
                provider_id=10,
                title="Movie",
                query="Movie",
            )
            result = workflow.request_movie(
                actor=actor,
                candidate_handle=handle,
                idempotency_key="handle-only",
            )
            self.assertEqual(result.intent.provider_id, 10)
            with self.assertRaises(ConflictError):
                workflow.request_movie(
                    11,
                    "Movie",
                    actor=actor,
                    candidate_handle=handle,
                    query="Movie",
                    idempotency_key="mismatched-id",
                )

    def test_unknown_add_reconciles_by_stable_provider_id_without_resend(self) -> None:
        actor = RequestActor(7, 8, update_id=200)
        with TemporaryDirectory() as directory:
            store = SQLiteRequestStore(Database(Path(directory) / "state.sqlite3"))
            provider = self._movie_provider(lost_response=True)
            workflow = RequestWorkflow(store=store, radarr=provider)
            handle = workflow.issue_candidate(
                actor=actor,
                media_type=MediaType.MOVIE,
                provider_id=10,
                title="Movie",
                query="Movie",
            )
            result = workflow.request_movie(
                10,
                "Movie",
                actor=actor,
                candidate_handle=handle,
                query="Movie",
                idempotency_key="update-200",
            )
            self.assertEqual(result.intent.status, RequestStatus.REQUESTED)
            self.assertEqual(
                store.list_commands(result.intent.request_id)[0].status, "unknown"
            )
            self.assertEqual(provider.calls, 1)
            reconciled = workflow.reconcile_pending()[0]
            self.assertEqual(reconciled.intent.status, RequestStatus.ACCEPTED)
            self.assertEqual(provider.calls, 1)

    def test_existing_plex_is_immediate_and_overlapping_requests_share_subscription(
        self,
    ) -> None:
        class Provider:
            def __init__(self) -> None:
                self.added = False

            def find_existing_movie(self, tmdb_id: int) -> object | None:
                return (
                    {"id": 1, "tmdbId": tmdb_id, "title": "Movie"}
                    if self.added
                    else None
                )

            def lookup_movie(
                self, tmdb_id: int, **kwargs: object
            ) -> list[dict[str, object]]:
                return [{"id": 1, "tmdbId": tmdb_id, "title": "Movie"}]

            def add_movie(self, movie: object, **kwargs: object) -> dict[str, object]:
                self.added = True
                return {"id": 1, "tmdbId": 10, "title": "Movie"}

        class Plex:
            def status_for_identity(self, identity: object) -> object:
                return SimpleNamespace(
                    available=True,
                    title="Movie",
                    year=2026,
                    plex_url="https://app.plex.tv/desktop/x",
                )

        actor = RequestActor(7, 8, update_id=300)
        with TemporaryDirectory() as directory:
            store = SQLiteRequestStore(Database(Path(directory) / "state.sqlite3"))
            workflow = RequestWorkflow(store=store, radarr=Provider(), plex=Plex())
            handle = workflow.issue_candidate(
                actor=actor,
                media_type=MediaType.MOVIE,
                provider_id=10,
                title="Movie",
                query="Movie",
            )
            result = workflow.request_movie(
                10,
                "Movie",
                actor=actor,
                candidate_handle=handle,
                query="Movie",
                idempotency_key="plex-1",
            )
            self.assertEqual(result.intent.status, RequestStatus.VISIBLE_IN_PLEX)
            self.assertEqual(result.commands, ())

            # A second request from the same destination has an independent
            # intent but attaches to the already canonical subscription.
            actor2 = RequestActor(7, 8, update_id=301)
            handle2 = workflow.issue_candidate(
                actor=actor2,
                media_type=MediaType.MOVIE,
                provider_id=10,
                title="Movie",
                query="Movie",
            )
            second = workflow.request_movie(
                10,
                "Movie",
                actor=actor2,
                candidate_handle=handle2,
                query="Movie",
                idempotency_key="plex-2",
            )
            self.assertEqual(second.subscriptions, result.subscriptions)

    def test_series_enumeration_handles_specials_and_uses_one_search_per_season(
        self,
    ) -> None:
        class Provider:
            def __init__(self) -> None:
                self.searches: list[int] = []

            def find_existing_series(self, tvdb_id: int) -> object:
                return {
                    "id": 44,
                    "tvdbId": tvdb_id,
                    "title": "Series",
                    "seasons": [{"seasonNumber": 0}, {"seasonNumber": 1}],
                }

            def lookup_series(
                self, tvdb_id: int, **kwargs: object
            ) -> list[dict[str, object]]:
                raise AssertionError("existing series should not be looked up")

            def add_series(self, series: object, **kwargs: object) -> object:
                raise AssertionError("existing series should not be added")

            def update_series(self, series: object, **kwargs: object) -> object:
                return {"id": 44, "tvdbId": 22, "title": "Series"}

            def search_season(
                self, series_id: int, season_number: int
            ) -> Mapping[str, object]:
                self.searches.append(season_number)
                return {"id": season_number}

            def get_series(self, series_id: int) -> Mapping[str, object]:
                return {"status": "ended"}

            def list_episode_records(
                self, series_id: int, *, season_number: int
            ) -> list[dict[str, object]]:
                if season_number == 0:
                    return [
                        {
                            "id": 100,
                            "seasonNumber": 0,
                            "episodeNumber": 1,
                            "title": "Special",
                            "hasFile": True,
                            "airDate": "2020-01-01",
                            "status": "released",
                        }
                    ]
                return [
                    {
                        "id": 101,
                        "seasonNumber": 1,
                        "episodeNumber": 1,
                        "title": "Future",
                        "hasFile": False,
                        "airDate": "2999-01-01",
                        "status": "continuing",
                    }
                ]

        actor = RequestActor(7, 8, update_id=400)
        with TemporaryDirectory() as directory:
            store = SQLiteRequestStore(Database(Path(directory) / "state.sqlite3"))
            provider = Provider()
            workflow = RequestWorkflow(store=store, sonarr=provider)
            handle = workflow.issue_candidate(
                actor=actor,
                media_type=MediaType.SERIES,
                provider_id=22,
                title="Series",
                query="Series",
            )
            result = workflow.request_series(
                seasons=[1, 0, 1],
                actor=actor,
                candidate_handle=handle,
                idempotency_key="series-modes",
            )
            self.assertEqual(provider.searches, [0, 1])
            self.assertEqual(result.intent.enumeration_versions, (1,))
            with store.database.connection() as connection:
                rows = connection.execute(
                    "SELECT season_number, mode FROM subscriptions ORDER BY season_number"
                ).fetchall()
            self.assertEqual(
                [(row[0], row[1]) for row in rows],
                [
                    (0, RequestMode.SEASON_COMPLETION.value),
                    (1, RequestMode.AIRING_EPISODE.value),
                ],
            )

    def test_intent_is_persisted_before_provider_mutation_and_is_idempotent(
        self,
    ) -> None:
        class Provider:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def find_existing_movie(self, tmdb_id: int) -> object | None:
                self.calls.append("find")
                return None

            def lookup_movie(
                self, tmdb_id: int, **kwargs: object
            ) -> list[dict[str, object]]:
                self.calls.append("lookup")
                return [{"id": 1, "tmdbId": tmdb_id, "title": "Movie"}]

            def add_movie(self, movie: object, **kwargs: object) -> dict[str, object]:
                self.calls.append("add")
                return {"id": 99, "tmdbId": 10, "title": "Movie"}

        with TemporaryDirectory() as directory:
            store = SQLiteRequestStore(Database(Path(directory) / "state.sqlite3"))
            provider = Provider()
            workflow = RequestWorkflow(
                store=store, radarr=provider, require_candidate_context=False
            )
            first = workflow.request_movie(
                10, actor=RequestActor(1, 2), idempotency_key="update-1"
            )
            second = workflow.request_movie(
                10, actor=RequestActor(1, 2), idempotency_key="update-1"
            )
            self.assertTrue(first.created)
            self.assertFalse(second.created)
            self.assertEqual(provider.calls, ["find", "lookup", "add"])
            self.assertEqual(first.intent.provider_item_id, "99")
            self.assertEqual(len(store.list_commands(first.intent.request_id)), 1)
            self.assertEqual(store.subscription_ids(first.intent.request_id), (1,))

    def test_series_request_creates_per_season_commands_not_episode_commands(
        self,
    ) -> None:
        class Provider:
            def find_existing_series(self, tvdb_id: int) -> object | None:
                return {
                    "id": 10,
                    "tvdbId": tvdb_id,
                    "title": "Series",
                    "seasons": [{"seasonNumber": 1}, {"seasonNumber": 2}],
                }

            def lookup_series(
                self, tvdb_id: int, **kwargs: object
            ) -> list[dict[str, object]]:
                raise AssertionError("existing series should not be looked up")

            def add_series(self, series: object, **kwargs: object) -> object:
                raise AssertionError("existing series should not be added")

            def update_series(self, series: object, **kwargs: object) -> object:
                return {
                    "id": 10,
                    "tvdbId": 2,
                    "title": "Series",
                    "seasons": [{"seasonNumber": 1}, {"seasonNumber": 2}],
                }

            def search_season(
                self, series_id: int, season_number: int
            ) -> dict[str, object]:
                return {"id": season_number}

        with TemporaryDirectory() as directory:
            store = SQLiteRequestStore(Database(Path(directory) / "state.sqlite3"))
            workflow = RequestWorkflow(
                store=store, sonarr=Provider(), require_candidate_context=False
            )
            result = workflow.request_series(
                2,
                seasons=[2, 1, 2],
                actor=RequestActor(1, 2),
                idempotency_key="series-1",
            )
            commands = store.list_commands(result.intent.request_id)
            self.assertEqual(
                [
                    command.season_number
                    for command in commands
                    if command.command_type == "season_search"
                ],
                [1, 2],
            )
            self.assertEqual(
                len(
                    [
                        command
                        for command in commands
                        if command.command_type == "season_search"
                    ]
                ),
                2,
            )


if __name__ == "__main__":
    unittest.main()
