from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import Any, Iterator
from unittest.mock import patch

import media_request_server as server
import radarr_webhook_bridge as webhook_bridge

SYNTHETIC_TELEGRAM_ID_1 = 900000001
SYNTHETIC_TELEGRAM_ID_2 = 900000002
SYNTHETIC_TELEGRAM_ID_3 = 900000003
SYNTHETIC_TELEGRAM_ID_4 = 900000004
SYNTHETIC_TELEGRAM_ID_5 = 900000005
SYNTHETIC_REQUESTER_1 = "synthetic-requester-1"
SYNTHETIC_REQUESTER_2 = "synthetic-requester-2"
SYNTHETIC_REQUESTER_3 = "synthetic-requester-3"
SYNTHETIC_REQUESTER_4 = "synthetic-requester-4"
SYNTHETIC_TIMESTAMP_1 = "2026-01-01T00:01:00+00:00"
SYNTHETIC_TIMESTAMP_2 = "2026-01-01T00:02:00+00:00"
SYNTHETIC_TIMESTAMP_3 = "2026-01-01T00:03:00+00:00"


@contextmanager
def sqlite_test_connection(
    path: str | os.PathLike[str],
) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


class FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code
        self.content = b"{}" if payload is not None else b""

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self.payload


class FakeSession:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append({"method": method, "url": url, **kwargs})
        return FakeResponse(self.responses.pop(0))


class FakeWebhookHandler:
    def __init__(
        self,
        body: bytes,
        headers: dict[str, str],
        max_body_bytes: int = webhook_bridge.DEFAULT_MAX_BODY_BYTES,
    ) -> None:
        self.headers = headers
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.server = types.SimpleNamespace(max_body_bytes=max_body_bytes)
        self.status: int | None = None
        self.response_headers: dict[str, str] = {}

    def send_response(self, status: int) -> None:
        self.status = status

    def send_header(self, name: str, value: str) -> None:
        self.response_headers[name] = value

    def end_headers(self) -> None:
        return None

    def response_json(self) -> dict[str, Any]:
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def config() -> server.ArrConfig:
    return server.ArrConfig(
        radarr_url="http://radarr:7878",
        radarr_api_key="radarr-key",
        radarr_quality_profile_id=501,
        radarr_quality_profile_name="Radarr Movie Profile",
        radarr_root_folder_path="/configured/movies",
        radarr_tag_ids=[11],
        sonarr_url="http://sonarr:8989",
        sonarr_api_key="sonarr-key",
        sonarr_normal_quality_profile_id=601,
        sonarr_normal_quality_profile_name="Sonarr Normal Profile",
        sonarr_anime_quality_profile_id=602,
        sonarr_anime_quality_profile_name="Sonarr Anime Profile",
        sonarr_root_folder_path="/configured/tv",
        sonarr_tag_ids=[21, 22],
    )


def env_config(overrides: dict[str, str] | None = None) -> dict[str, str]:
    values = {
        server.ENV_RADARR_URL: "http://radarr:7878",
        server.ENV_RADARR_API_KEY: "radarr-key",
        server.ENV_RADARR_QUALITY_PROFILE_ID: "501",
        server.ENV_RADARR_QUALITY_PROFILE_NAME: "Radarr Movie Profile",
        server.ENV_RADARR_ROOT_FOLDER_PATH: "/configured/movies",
        server.ENV_RADARR_TAG_IDS: "11",
        server.ENV_SONARR_URL: "http://sonarr:8989",
        server.ENV_SONARR_API_KEY: "sonarr-key",
        server.ENV_SONARR_NORMAL_QUALITY_PROFILE_ID: "601",
        server.ENV_SONARR_NORMAL_QUALITY_PROFILE_NAME: "Sonarr Normal Profile",
        server.ENV_SONARR_ANIME_QUALITY_PROFILE_ID: "602",
        server.ENV_SONARR_ANIME_QUALITY_PROFILE_NAME: "Sonarr Anime Profile",
        server.ENV_SONARR_ROOT_FOLDER_PATH: "/configured/tv",
        server.ENV_SONARR_TAG_IDS: "21, 22",
    }
    if overrides:
        values.update(overrides)
    return values


class ConfigTests(unittest.TestCase):
    def test_load_config_uses_project_scoped_env_names(self) -> None:
        loaded = server.load_config(
            env_config(
                {
                    server.ENV_RADARR_URL: " http://radarr:7878/ ",
                    server.ENV_RADARR_API_KEY: " radarr-key ",
                    server.ENV_SONARR_URL: "http://sonarr:8989/",
                }
            )
        )

        self.assertEqual(loaded.radarr_url, "http://radarr:7878")
        self.assertEqual(loaded.radarr_api_key, "radarr-key")
        self.assertEqual(loaded.sonarr_url, "http://sonarr:8989")
        self.assertEqual(loaded.radarr_quality_profile_id, 501)
        self.assertEqual(loaded.sonarr_anime_quality_profile_id, 602)
        self.assertEqual(loaded.radarr_tag_ids, [11])
        self.assertEqual(loaded.sonarr_tag_ids, [21, 22])

    def test_load_config_fails_clearly_for_missing_values(self) -> None:
        with self.assertRaisesRegex(RuntimeError, server.ENV_RADARR_URL):
            server.load_config({})

    def test_load_config_requires_arr_urls(self) -> None:
        with self.assertRaisesRegex(RuntimeError, server.ENV_RADARR_URL):
            server.load_config(
                env_config(
                    {
                        server.ENV_RADARR_URL: " ",
                        server.ENV_SONARR_URL: "",
                    }
                )
            )

    def test_load_config_requires_arr_api_keys(self) -> None:
        with self.assertRaisesRegex(RuntimeError, server.ENV_RADARR_API_KEY):
            server.load_config(
                env_config(
                    {
                        server.ENV_RADARR_API_KEY: "",
                        server.ENV_SONARR_API_KEY: "",
                    }
                )
            )

    def test_load_config_normalizes_arr_urls(self) -> None:
        loaded = server.load_config(
            env_config(
                {
                    server.ENV_RADARR_URL: " http://radarr:7878/ ",
                    server.ENV_SONARR_URL: "http://sonarr:8989/",
                }
            )
        )

        self.assertEqual(loaded.radarr_url, "http://radarr:7878")
        self.assertEqual(loaded.sonarr_url, "http://sonarr:8989")

    def test_load_config_requires_numeric_profile_ids(self) -> None:
        with self.assertRaisesRegex(RuntimeError, server.ENV_RADARR_QUALITY_PROFILE_ID):
            server.load_config(
                env_config({server.ENV_RADARR_QUALITY_PROFILE_ID: "not-a-number"})
            )

    def test_load_config_allows_empty_tag_ids(self) -> None:
        loaded = server.load_config(
            env_config(
                {
                    server.ENV_RADARR_TAG_IDS: "",
                    server.ENV_SONARR_TAG_IDS: "",
                }
            )
        )

        self.assertEqual(loaded.radarr_tag_ids, [])
        self.assertEqual(loaded.sonarr_tag_ids, [])

    def test_load_config_requires_numeric_tag_ids(self) -> None:
        with self.assertRaisesRegex(RuntimeError, server.ENV_SONARR_TAG_IDS):
            server.load_config(env_config({server.ENV_SONARR_TAG_IDS: "agent"}))


class SearchMediaTests(unittest.TestCase):
    def test_search_media_reports_movie_available_and_missing(self) -> None:
        session = FakeSession(
            [
                [
                    {
                        "title": "Dune",
                        "year": 2021,
                        "tmdbId": 438631,
                        "imdbId": "tt1160419",
                        "runtime": 155,
                    },
                    {
                        "title": "Missing Movie",
                        "year": 2024,
                        "tmdbId": 999,
                    },
                ],
                [
                    {"title": "Dune", "year": 2021, "tmdbId": 438631, "hasFile": True},
                    {
                        "title": "Missing Movie",
                        "year": 2024,
                        "tmdbId": 999,
                        "hasFile": False,
                    },
                ],
            ]
        )
        service = server.MediaRequestService(config(), session=session)

        result = service.search_media("dune", media_type="movie")

        self.assertTrue(result["ok"])
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(
            result["items"][0],
            {
                "media_type": "movie",
                "title": "Dune",
                "year": 2021,
                "tmdbId": 438631,
                "exists": True,
                "available": True,
                "imdbId": "tt1160419",
                "runtimeMinutes": 155,
            },
        )
        self.assertTrue(result["items"][1]["exists"])
        self.assertFalse(result["items"][1]["available"])

    def test_search_media_reports_series_requested_season_available(self) -> None:
        session = FakeSession(
            [
                [
                    {
                        "title": "My Brilliant Friend",
                        "year": 2018,
                        "tvdbId": 344626,
                        "seasons": [{"seasonNumber": 1}, {"seasonNumber": 4}],
                    }
                ],
                [
                    {
                        "title": "My Brilliant Friend",
                        "year": 2018,
                        "tvdbId": 344626,
                        "seasons": [
                            {
                                "seasonNumber": 1,
                                "statistics": {
                                    "episodeFileCount": 8,
                                    "episodeCount": 8,
                                },
                            },
                            {
                                "seasonNumber": 4,
                                "statistics": {
                                    "episodeFileCount": 0,
                                    "episodeCount": 10,
                                },
                            },
                        ],
                    }
                ],
            ]
        )
        service = server.MediaRequestService(config(), session=session)

        result = service.search_media(
            "my brilliant SYNTHETIC_REQUESTER_4", media_type="series", season=1
        )

        item = result["items"][0]
        self.assertTrue(item["available"])
        self.assertEqual(item["seasons"], [1, 4])
        self.assertEqual(
            item["availability"],
            {
                "availableEpisodes": 8,
                "missingEpisodes": 0,
                "totalEpisodes": 8,
                "seasons": [
                    {
                        "season": 1,
                        "available": True,
                        "availableEpisodes": 8,
                        "missingEpisodes": 0,
                        "totalEpisodes": 8,
                    }
                ],
            },
        )

    def test_search_media_reports_series_requested_season_missing(self) -> None:
        session = FakeSession(
            [
                [
                    {
                        "title": "My Brilliant Friend",
                        "year": 2018,
                        "tvdbId": 344626,
                        "seasons": [{"seasonNumber": 1}, {"seasonNumber": 4}],
                    }
                ],
                [
                    {
                        "title": "My Brilliant Friend",
                        "year": 2018,
                        "tvdbId": 344626,
                        "seasons": [
                            {
                                "seasonNumber": 4,
                                "statistics": {
                                    "episodeFileCount": 0,
                                    "episodeCount": 10,
                                },
                            }
                        ],
                    }
                ],
            ]
        )
        service = server.MediaRequestService(config(), session=session)

        result = service.search_media(
            "my brilliant SYNTHETIC_REQUESTER_4", media_type="series", season=4
        )

        item = result["items"][0]
        self.assertTrue(item["exists"])
        self.assertFalse(item["available"])
        self.assertEqual(item["availability"]["availableEpisodes"], 0)
        self.assertEqual(item["availability"]["missingEpisodes"], 10)

    def test_search_media_does_not_treat_series_metadata_as_available(self) -> None:
        session = FakeSession(
            [
                [
                    {
                        "title": "Metadata Only Show",
                        "year": 2024,
                        "tvdbId": 123,
                        "seasons": [{"seasonNumber": 1}, {"seasonNumber": 2}],
                    }
                ],
                [
                    {
                        "title": "Metadata Only Show",
                        "year": 2024,
                        "tvdbId": 123,
                        "seasons": [{"seasonNumber": 1}, {"seasonNumber": 2}],
                    }
                ],
            ]
        )
        service = server.MediaRequestService(config(), session=session)

        result = service.search_media("metadata", media_type="series")

        item = result["items"][0]
        self.assertTrue(item["exists"])
        self.assertFalse(item["available"])
        self.assertEqual(item["availability"]["availableEpisodes"], 0)


class DownloadStatusTests(unittest.TestCase):
    def test_download_status_normalizes_radarr_queue_items(self) -> None:
        session = FakeSession(
            [
                {
                    "records": [
                        {
                            "movie": {"title": "Dune"},
                            "status": "downloading",
                            "size": 1000,
                            "sizeleft": 250,
                            "timeleft": "00:30:00",
                            "trackedDownloadStatus": "ok",
                            "trackedDownloadState": "downloading",
                            "downloadClient": "SABnzbd",
                        }
                    ]
                },
                {"records": []},
            ]
        )
        service = server.MediaRequestService(config(), session=session)

        result = service.download_status()

        self.assertEqual(
            result,
            {
                "active": True,
                "items": [
                    {
                        "media_type": "movie",
                        "title": "Dune",
                        "status": "downloading",
                        "progress_percent": 75.0,
                        "time_left": "00:30:00",
                        "tracked_download_status": "ok",
                        "tracked_download_state": "downloading",
                        "download_client": "SABnzbd",
                    }
                ],
            },
        )
        self.assertEqual(
            [
                (request["method"], request["url"], request["params"])
                for request in session.requests
            ],
            [
                ("GET", "http://radarr:7878/api/v3/queue", server.QUEUE_PARAMS),
                ("GET", "http://sonarr:8989/api/v3/queue", server.QUEUE_PARAMS),
            ],
        )

    def test_download_status_normalizes_sonarr_queue_items(self) -> None:
        session = FakeSession(
            [
                {"records": []},
                {
                    "records": [
                        {
                            "series": {"title": "Fringe"},
                            "status": "completed",
                            "progress": 100,
                            "timeLeft": "00:00:00",
                            "trackedDownloadStatus": "warning",
                            "trackedDownloadState": "importPending",
                            "downloadClientName": "qBittorrent",
                        }
                    ]
                },
            ]
        )
        service = server.MediaRequestService(config(), session=session)

        result = service.download_status()

        self.assertEqual(
            result,
            {
                "active": True,
                "items": [
                    {
                        "media_type": "series",
                        "title": "Fringe",
                        "status": "completed",
                        "progress_percent": 100.0,
                        "time_left": "00:00:00",
                        "tracked_download_status": "warning",
                        "tracked_download_state": "importPending",
                        "download_client": "qBittorrent",
                        "note": "Download is complete and waiting to be imported.",
                    }
                ],
            },
        )

    def test_download_status_returns_empty_summary_for_empty_queues(self) -> None:
        session = FakeSession([{"records": []}, {"records": []}])
        service = server.MediaRequestService(config(), session=session)

        result = service.download_status()

        self.assertEqual(
            result,
            {
                "active": False,
                "items": [],
                "message": "No active downloads found.",
            },
        )

    def test_download_status_does_not_leak_secret_urls_or_paths(self) -> None:
        session = FakeSession(
            [
                {
                    "records": [
                        {
                            "movie": {"title": "Dune"},
                            "status": "downloading",
                            "downloadUrl": "https://download.example/secret",
                            "indexer": "Private Indexer",
                            "outputPath": "/downloads/secret/Dune.mkv",
                            "downloadClient": "http://internal-client:8080",
                            "trackedDownloadStatus": "ok",
                            "trackedDownloadState": "downloading",
                        }
                    ]
                },
                {"records": []},
            ]
        )
        service = server.MediaRequestService(config(), session=session)

        serialized = json.dumps(service.download_status())

        self.assertNotIn("secret", serialized)
        self.assertNotIn("download.example", serialized)
        self.assertNotIn("/downloads", serialized)
        self.assertNotIn("Private Indexer", serialized)
        self.assertNotIn("internal-client", serialized)


class RequestStatusTests(unittest.TestCase):
    def test_request_status_available_match_uses_radarr_title_keys_and_year(
        self,
    ) -> None:
        movies = [
            {
                "id": 1,
                "title": "Different Display Title",
                "cleanTitle": "cloudatlas",
                "titleSlug": "cloud-atlas-2012",
                "year": 2012,
                "hasFile": True,
                "alternateTitles": [{"title": "Atlas des nuages"}],
            },
            {
                "id": 2,
                "title": "Cloud Atlas",
                "cleanTitle": "cloudatlas",
                "titleSlug": "cloud-atlas-2013",
                "year": 2013,
                "hasFile": True,
            },
        ]
        session = FakeSession([{"records": []}, {"records": []}, movies, []])
        service = server.MediaRequestService(config(), session=session)

        result = service.request_status(query="Cloud Atlas 2012")

        self.assertEqual(result["title"], "Different Display Title")
        self.assertEqual(result["year"], 2012)
        self.assertEqual(result["state"], "available")

    def test_request_status_marks_unreleased_movie_as_waiting_for_release(self) -> None:
        session = FakeSession(
            [
                {"records": []},
                {"records": []},
                [
                    {
                        "id": 42,
                        "title": "Future Movie",
                        "monitored": True,
                        "hasFile": False,
                        "physicalRelease": "2999-01-01T00:00:00Z",
                    }
                ],
                [],
            ]
        )
        service = server.MediaRequestService(config(), session=session)

        result = service.request_status()

        self.assertEqual(
            result,
            {
                "active": False,
                "items": [
                    {
                        "media_type": "movie",
                        "status": "waiting_for_release",
                        "eta": None,
                        "message": (
                            "This is being watched, but it has not been released yet. "
                            "No ETA is available until a download starts."
                        ),
                        "title": "Future Movie",
                    }
                ],
            },
        )
        self.assertNotIn("progress_percent", result["items"][0])
        self.assertNotIn("time_left", result["items"][0])

    def test_request_status_marks_released_movie_as_waiting_for_suitable_release(
        self,
    ) -> None:
        session = FakeSession(
            [
                {"records": []},
                {"records": []},
                [
                    {
                        "id": 42,
                        "title": "Past Movie",
                        "monitored": True,
                        "hasFile": False,
                        "physicalRelease": SYNTHETIC_TIMESTAMP_1,
                    }
                ],
                [],
            ]
        )
        service = server.MediaRequestService(config(), session=session)

        result = service.request_status()

        self.assertEqual(result["items"][0]["status"], "waiting_for_suitable_release")
        self.assertIsNone(result["items"][0]["eta"])
        self.assertEqual(
            result["items"][0]["message"],
            (
                "This is being watched, but no suitable release has been found yet. "
                "No ETA is available until a download starts."
            ),
        )
        self.assertNotIn("progress_percent", result["items"][0])
        self.assertNotIn("time_left", result["items"][0])

    def test_request_status_marks_future_series_as_waiting_for_release(self) -> None:
        session = FakeSession(
            [
                {"records": []},
                {"records": []},
                [],
                [
                    {
                        "id": 7,
                        "title": "Future Show",
                        "monitored": True,
                        "statistics": {"episodeFileCount": 0},
                        "firstAired": "2999-01-01T00:00:00Z",
                    }
                ],
            ]
        )
        service = server.MediaRequestService(config(), session=session)

        result = service.request_status()

        self.assertEqual(result["items"][0]["media_type"], "series")
        self.assertEqual(result["items"][0]["status"], "waiting_for_release")
        self.assertIsNone(result["items"][0]["eta"])

    def test_request_status_only_returns_eta_for_active_downloads(self) -> None:
        session = FakeSession(
            [
                {
                    "records": [
                        {
                            "movieId": 42,
                            "movie": {"title": "Dune"},
                            "status": "downloading",
                            "progress": 50,
                            "timeleft": "00:10:00",
                            "trackedDownloadState": "downloading",
                        }
                    ]
                },
                {
                    "records": [
                        {
                            "seriesId": 7,
                            "series": {"title": "Fringe"},
                            "status": "completed",
                            "progress": 100,
                            "timeleft": "00:00:00",
                            "trackedDownloadState": "importPending",
                        }
                    ]
                },
                [{"id": 42, "title": "Dune", "monitored": True, "hasFile": False}],
                [
                    {
                        "id": 7,
                        "title": "Fringe",
                        "monitored": True,
                        "statistics": {"episodeFileCount": 0},
                    }
                ],
            ]
        )
        service = server.MediaRequestService(config(), session=session)

        result = service.request_status()

        self.assertTrue(result["active"])
        self.assertEqual(result["items"][0]["status"], "downloading")
        self.assertEqual(result["items"][0]["eta"], "00:10:00")
        self.assertEqual(result["items"][0]["progress_percent"], 50.0)
        self.assertEqual(result["items"][0]["time_left"], "00:10:00")
        self.assertEqual(result["items"][1]["status"], "importPending")
        self.assertIsNone(result["items"][1]["eta"])
        self.assertNotIn("progress_percent", result["items"][1])
        self.assertNotIn("time_left", result["items"][1])

    def test_request_status_filters_by_query(self) -> None:
        session = FakeSession(
            [
                {"records": []},
                {"records": []},
                [
                    {
                        "id": 42,
                        "title": "Dune",
                        "monitored": True,
                        "hasFile": False,
                        "physicalRelease": SYNTHETIC_TIMESTAMP_1,
                    },
                    {
                        "id": 43,
                        "title": "Alien",
                        "monitored": True,
                        "hasFile": False,
                        "physicalRelease": SYNTHETIC_TIMESTAMP_1,
                    },
                ],
                [],
            ]
        )
        service = server.MediaRequestService(config(), session=session)

        result = service.request_status(query="alien")

        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["title"], "Alien")

    def test_request_status_default_limit_can_return_more_than_ten_queue_items(
        self,
    ) -> None:
        queue_records = [
            {
                "movieId": index,
                "movie": {"title": f"Movie {index}"},
                "status": "downloading",
                "progress": 10,
                "trackedDownloadState": "downloading",
            }
            for index in range(12)
        ]
        session = FakeSession([{"records": queue_records}, {"records": []}, [], []])
        service = server.MediaRequestService(config(), session=session)

        result = service.request_status()

        self.assertEqual(len(result["items"]), 12)
        self.assertEqual(
            [(request["url"], request["params"]) for request in session.requests[:2]],
            [
                ("http://radarr:7878/api/v3/queue", server.QUEUE_PARAMS),
                ("http://sonarr:8989/api/v3/queue", server.QUEUE_PARAMS),
            ],
        )


class LibraryToolTests(unittest.TestCase):
    def test_browse_library_filters_movies_by_genre(self) -> None:
        session = FakeSession(
            [
                [
                    {
                        "title": "Heat",
                        "year": 1995,
                        "genres": ["Crime", "Drama"],
                        "runtime": 170,
                        "overview": "A detective tracks a crew of thieves.",
                        "imdbId": "tt0113277",
                        "tmdbId": 949,
                        "hasFile": True,
                        "images": [
                            {
                                "coverType": "poster",
                                "remoteUrl": "https://image.tmdb.org/heat.jpg",
                            }
                        ],
                    },
                    {
                        "title": "Galaxy Quest",
                        "year": 1999,
                        "genres": ["Comedy"],
                        "runtime": 102,
                        "hasFile": True,
                    },
                ]
            ]
        )
        service = server.MediaRequestService(config(), session=session)

        results = service.browse_library(media_type="movie", genre="Crime")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "Heat")
        self.assertEqual(results[0]["media_type"], "movie")
        self.assertEqual(results[0]["runtimeMinutes"], 170)
        self.assertEqual(results[0]["imdbId"], "tt0113277")
        self.assertEqual(results[0]["tmdbId"], 949)
        self.assertEqual(results[0]["posterUrl"], "https://image.tmdb.org/heat.jpg")
        self.assertTrue(results[0]["available"])

    def test_browse_library_filters_series_by_genre(self) -> None:
        session = FakeSession(
            [
                [
                    {
                        "title": "The Wire",
                        "year": 2002,
                        "genres": ["Crime", "Drama"],
                        "status": "ended",
                        "overview": "Baltimore institutions and crime.",
                        "imdbId": "tt0306414",
                        "tmdbId": 1438,
                        "tvdbId": 79126,
                        "statistics": {"episodeFileCount": 60},
                        "seasons": [
                            {
                                "seasonNumber": 1,
                                "statistics": {
                                    "episodeFileCount": 13,
                                    "episodeCount": 13,
                                },
                            },
                            {
                                "seasonNumber": 2,
                                "statistics": {
                                    "episodeFileCount": 12,
                                    "episodeCount": 12,
                                },
                            },
                        ],
                    },
                    {
                        "title": "Unavailable Show",
                        "year": 2024,
                        "genres": ["Drama"],
                        "statistics": {"episodeFileCount": 0},
                    },
                ]
            ]
        )
        service = server.MediaRequestService(config(), session=session)

        results = service.browse_library(media_type="series", genre="Crime")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "The Wire")
        self.assertEqual(results[0]["media_type"], "series")
        self.assertEqual(results[0]["seasons"], [1, 2])
        self.assertEqual(results[0]["tvdbId"], 79126)
        self.assertEqual(results[0]["imdbId"], "tt0306414")
        self.assertEqual(results[0]["tmdbId"], 1438)
        self.assertEqual(results[0]["availability"]["availableEpisodes"], 25)
        self.assertTrue(results[0]["available"])

    def test_browse_library_excludes_unavailable_movies(self) -> None:
        session = FakeSession(
            [
                [
                    {
                        "title": "Available Movie",
                        "year": 2001,
                        "genres": ["Drama"],
                        "hasFile": True,
                    },
                    {
                        "title": "Unavailable Movie",
                        "year": 2002,
                        "genres": ["Drama"],
                        "hasFile": False,
                    },
                ]
            ]
        )
        service = server.MediaRequestService(config(), session=session)

        results = service.browse_library(media_type="movie", genre="Drama")

        self.assertEqual([item["title"] for item in results], ["Available Movie"])

    def test_browse_library_excludes_unavailable_series(self) -> None:
        session = FakeSession(
            [
                [
                    {
                        "title": "Available Show",
                        "year": 2001,
                        "genres": ["Drama"],
                        "seasons": [
                            {
                                "seasonNumber": 1,
                                "statistics": {
                                    "episodeFileCount": 1,
                                    "episodeCount": 10,
                                },
                            }
                        ],
                    },
                    {
                        "title": "Unavailable Show",
                        "year": 2002,
                        "genres": ["Drama"],
                        "statistics": {"episodeFileCount": 0},
                        "seasons": [{"seasonNumber": 1}],
                    },
                ]
            ]
        )
        service = server.MediaRequestService(config(), session=session)

        results = service.browse_library(media_type="series", genre="Drama")

        self.assertEqual([item["title"] for item in results], ["Available Show"])

    def test_library_output_is_sanitized(self) -> None:
        session = FakeSession(
            [
                [
                    {
                        "title": "Safe Movie",
                        "year": 2001,
                        "genres": ["Drama"],
                        "hasFile": True,
                        "rootFolderPath": "/data/media/movies",
                        "path": "/data/media/movies/Safe Movie",
                        "movieFile": {"path": "/downloads/Safe Movie.mkv"},
                        "images": [
                            {
                                "coverType": "poster",
                                "remoteUrl": "http://radarr:7878/MediaCover/1.jpg",
                            }
                        ],
                    }
                ]
            ]
        )
        service = server.MediaRequestService(config(), session=session)

        serialized = json.dumps(service.browse_library(media_type="movie"))

        self.assertNotIn("/data/media", serialized)
        self.assertNotIn("/downloads", serialized)
        self.assertNotIn("radarr:7878", serialized)
        self.assertNotIn("rootFolderPath", serialized)
        self.assertNotIn("movieFile", serialized)


class McpToolTests(unittest.TestCase):
    def test_create_server_registers_expected_tools(self) -> None:
        class FakeFastMCP:
            def __init__(self, name: str) -> None:
                self.name = name
                self.tools: list[str] = []

            def tool(self) -> Any:
                def decorator(fn: Any) -> Any:
                    self.tools.append(fn.__name__)
                    return fn

                return decorator

        mcp_module = types.ModuleType("mcp")
        server_module = types.ModuleType("mcp.server")
        fastmcp_module = types.ModuleType("mcp.server.fastmcp")
        fastmcp_module.FastMCP = FakeFastMCP

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.dict(
                    sys.modules,
                    {
                        "mcp": mcp_module,
                        "mcp.server": server_module,
                        "mcp.server.fastmcp": fastmcp_module,
                    },
                ),
                patch.dict(
                    os.environ,
                    env_config(
                        {
                            server.ENV_REQUEST_DB_PATH: os.path.join(
                                directory, "requests.sqlite3"
                            )
                        }
                    ),
                    clear=True,
                ),
            ):
                mcp = server.create_server()

        self.assertEqual(
            mcp.tools,
            [
                "search_media",
                "request_movie",
                "request_series",
                "request_status",
                "download_status",
                "repair_blocked_imports",
                "browse_library",
                "media_status",
                "notify_movie_available",
                "notify_series_available",
                "notify_available_requests",
            ],
        )


class RequestSeriesTests(unittest.TestCase):
    def test_request_series_requires_explicit_non_empty_seasons(self) -> None:
        service = server.MediaRequestService(config(), session=FakeSession([]))

        missing = service.request_series(123)
        empty = service.request_series(123, seasons=[])

        self.assertEqual(missing["status"], "error")
        self.assertIn("explicit non-empty", missing["message"])
        self.assertEqual(empty["status"], "error")
        self.assertIn("explicit non-empty", empty["message"])

    def test_request_series_whole_show_requires_explicit_seasons(self) -> None:
        session = FakeSession(
            [
                [],
                [
                    {
                        "title": "Whole Show",
                        "tvdbId": 123,
                        "seasons": [
                            {"seasonNumber": 0},
                            {"seasonNumber": 1},
                            {"seasonNumber": 2},
                        ],
                    }
                ],
                {"title": "Whole Show", "tvdbId": 123},
            ]
        )
        service = server.MediaRequestService(config(), session=session)

        result = service.request_series(123, seasons=[1, 2])

        self.assertEqual(result["status"], "added")
        self.assertEqual(result["monitoredSeasons"], [1, 2])
        post = session.requests[-1]
        self.assertEqual(
            post["json"]["seasons"],
            [
                {"seasonNumber": 0, "monitored": False},
                {"seasonNumber": 1, "monitored": True},
                {"seasonNumber": 2, "monitored": True},
            ],
        )
        self.assertEqual(post["json"]["qualityProfileId"], 601)
        self.assertEqual(post["json"]["rootFolderPath"], "/configured/tv")
        self.assertEqual(post["json"]["tags"], [21, 22])

    def test_request_series_existing_returns_requested_season_counts(self) -> None:
        session = FakeSession(
            [
                [
                    {
                        "title": "Existing Show",
                        "tvdbId": 123,
                        "seasons": [
                            {
                                "seasonNumber": 1,
                                "statistics": {
                                    "episodeFileCount": 2,
                                    "episodeCount": 10,
                                },
                            },
                            {
                                "seasonNumber": 2,
                                "statistics": {
                                    "episodeFileCount": 0,
                                    "episodeCount": 8,
                                },
                            },
                        ],
                    }
                ]
            ]
        )
        service = server.MediaRequestService(config(), session=session)

        result = service.request_series(123, seasons=[2])

        self.assertEqual(result["status"], "already_exists")
        self.assertEqual(result["monitoredSeasons"], [2])
        self.assertFalse(result["available"])
        self.assertEqual(
            result["availability"],
            {
                "availableEpisodes": 0,
                "missingEpisodes": 8,
                "totalEpisodes": 8,
                "seasons": [
                    {
                        "season": 2,
                        "available": False,
                        "availableEpisodes": 0,
                        "missingEpisodes": 8,
                        "totalEpisodes": 8,
                    }
                ],
            },
        )
        self.assertEqual(len(session.requests), 1)

    def test_request_series_existing_adds_monitoring_and_starts_season_search(
        self,
    ) -> None:
        existing = {
            "id": 77,
            "title": "Existing Show",
            "tvdbId": 123,
            "monitored": True,
            "seasons": [
                {
                    "seasonNumber": 1,
                    "monitored": True,
                    "statistics": {"episodeFileCount": 2, "episodeCount": 10},
                },
                {
                    "seasonNumber": 2,
                    "monitored": False,
                    "statistics": {"episodeFileCount": 0, "episodeCount": 8},
                },
            ],
        }
        updated = {
            **existing,
            "seasons": [
                {
                    "seasonNumber": 1,
                    "monitored": True,
                    "statistics": {"episodeFileCount": 2, "episodeCount": 10},
                },
                {
                    "seasonNumber": 2,
                    "monitored": True,
                    "statistics": {"episodeFileCount": 0, "episodeCount": 8},
                },
            ],
        }
        session = FakeSession([[existing], updated, {"id": 99, "name": "SeasonSearch"}])
        service = server.MediaRequestService(config(), session=session)

        result = service.request_series(123, seasons=[2])

        self.assertEqual(result["status"], "already_exists")
        self.assertTrue(result["monitoringUpdated"])
        self.assertTrue(result["searchSubmitted"])
        self.assertEqual(result["monitoredSeasons"], [2])
        self.assertEqual(
            [request["method"] for request in session.requests], ["GET", "PUT", "POST"]
        )
        put = session.requests[1]
        self.assertEqual(put["url"], "http://sonarr:8989/api/v3/series/77")
        self.assertEqual(
            put["json"]["seasons"],
            [
                {
                    "seasonNumber": 1,
                    "monitored": True,
                    "statistics": {"episodeFileCount": 2, "episodeCount": 10},
                },
                {
                    "seasonNumber": 2,
                    "monitored": True,
                    "statistics": {"episodeFileCount": 0, "episodeCount": 8},
                },
            ],
        )
        self.assertEqual(
            session.requests[2]["json"],
            {"name": "SeasonSearch", "seriesId": 77, "seasonNumber": 2},
        )

    def test_existing_anime_series_applies_profile_and_keeps_state_on_numeric_put(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = server.RequestStore(os.path.join(directory, "requests.sqlite3"))
            existing = {
                "id": 77,
                "title": "Example Anime",
                "tvdbId": 7001,
                "qualityProfileId": 1,
                "seasons": [
                    {
                        "seasonNumber": 1,
                        "monitored": False,
                        "statistics": {
                            "episodeFileCount": 1,
                            "totalEpisodeCount": 12,
                        },
                    }
                ],
            }
            session = FakeSession([[existing], 77, {"id": 99}])
            service = server.MediaRequestService(
                config(), session=session, request_store=store
            )

            result = service.request_series(
                7001,
                seasons=[1],
                anime=True,
                requested_by_chat_id=SYNTHETIC_TELEGRAM_ID_1,
            )

            self.assertEqual(result["status"], "already_exists")
            self.assertEqual(result["profileUsed"], "Sonarr Anime Profile")
            self.assertEqual(session.requests[1]["json"]["qualityProfileId"], 602)
            with sqlite_test_connection(store.db_path) as connection:
                row = connection.execute(
                    "SELECT sonarr_series_id, status, notified_available_at "
                    "FROM media_requests"
                ).fetchone()
            self.assertEqual(row, (77, "requested", None))


class RequestPolicyTests(unittest.TestCase):
    def test_request_movie_includes_immediate_post_request_status(self) -> None:
        session = FakeSession(
            [
                [],
                [{"title": "Alien", "tmdbId": 348, "titleSlug": "alien-1979"}],
                {"title": "Alien", "tmdbId": 348},
                {
                    "records": [
                        {
                            "movieId": 348,
                            "movie": {"title": "Alien"},
                            "status": "downloading",
                            "progress": 25,
                            "timeleft": "00:12:00",
                            "trackedDownloadState": "downloading",
                        }
                    ]
                },
                {"records": []},
                [{"id": 348, "title": "Alien", "monitored": True, "hasFile": False}],
                [],
            ]
        )
        service = server.MediaRequestService(config(), session=session)

        result = service.request_movie(348)

        self.assertEqual(result["status"], "added")
        self.assertEqual(
            result["postRequestStatus"]["items"][0]["status"], "downloading"
        )
        self.assertEqual(result["postRequestStatus"]["items"][0]["eta"], "00:12:00")

    def test_request_series_includes_immediate_post_request_status(self) -> None:
        session = FakeSession(
            [
                [],
                [
                    {
                        "title": "Fringe",
                        "tvdbId": 82066,
                        "titleSlug": "fringe",
                        "seasons": [{"seasonNumber": 1}],
                    }
                ],
                {"title": "Fringe", "tvdbId": 82066},
                {"records": []},
                {
                    "records": [
                        {
                            "seriesId": 7,
                            "series": {"title": "Fringe"},
                            "status": "downloading",
                            "progress": 50,
                            "timeleft": "00:10:00",
                            "trackedDownloadState": "downloading",
                        }
                    ]
                },
                [],
                [
                    {
                        "id": 7,
                        "title": "Fringe",
                        "monitored": True,
                        "statistics": {"episodeFileCount": 0},
                    }
                ],
            ]
        )
        service = server.MediaRequestService(config(), session=session)

        result = service.request_series(82066, seasons=[1])

        self.assertEqual(result["status"], "added")
        self.assertEqual(
            result["postRequestStatus"]["items"][0]["status"], "downloading"
        )
        self.assertEqual(result["postRequestStatus"]["items"][0]["eta"], "00:10:00")

    def test_request_movie_enforces_configured_radarr_policy(self) -> None:
        session = FakeSession(
            [
                [],
                [{"title": "Alien", "tmdbId": 348, "titleSlug": "alien-1979"}],
                {"title": "Alien", "tmdbId": 348},
            ]
        )
        service = server.MediaRequestService(config(), session=session)

        result = service.request_movie(348)

        self.assertEqual(result["status"], "added")
        post = session.requests[-1]
        self.assertEqual(post["method"], "POST")
        self.assertEqual(post["json"]["qualityProfileId"], 501)
        self.assertEqual(post["json"]["rootFolderPath"], "/configured/movies")
        self.assertTrue(post["json"]["monitored"])
        self.assertEqual(post["json"]["minimumAvailability"], "announced")
        self.assertEqual(post["json"]["tags"], [11])

    def test_request_movie_reports_existing(self) -> None:
        session = FakeSession([[{"title": "Alien", "tmdbId": 348}]])
        service = server.MediaRequestService(config(), session=session)

        result = service.request_movie(348)

        self.assertEqual(result["status"], "already_exists")
        self.assertEqual(len(session.requests), 1)

    def test_request_movie_repairs_missing_existing_movie_and_starts_search(
        self,
    ) -> None:
        existing = {
            "id": 44,
            "title": "Alien",
            "tmdbId": 348,
            "hasFile": False,
            "monitored": False,
            "qualityProfileId": 1,
        }
        updated = {
            **existing,
            "monitored": True,
            "qualityProfileId": 501,
        }
        session = FakeSession([[existing], updated, {"id": 99}])
        service = server.MediaRequestService(config(), session=session)

        result = service.request_movie(348)

        self.assertEqual(result["status"], "already_exists")
        self.assertTrue(result["monitoringUpdated"])
        self.assertTrue(result["searchSubmitted"])
        self.assertEqual(
            [request["method"] for request in session.requests],
            ["GET", "PUT", "POST"],
        )
        self.assertTrue(session.requests[1]["json"]["monitored"])
        self.assertEqual(session.requests[1]["json"]["qualityProfileId"], 501)
        self.assertEqual(
            session.requests[2]["json"],
            {"name": "MoviesSearch", "movieIds": [44]},
        )

    def test_request_series_enforces_normal_profile(self) -> None:
        session = FakeSession(
            [
                [],
                [
                    {
                        "title": "Fringe",
                        "tvdbId": 82066,
                        "titleSlug": "fringe",
                        "seasons": [{"seasonNumber": 1}],
                    }
                ],
                {"title": "Fringe", "tvdbId": 82066},
            ]
        )
        service = server.MediaRequestService(config(), session=session)

        result = service.request_series(82066, seasons=[1])

        self.assertEqual(result["status"], "added")
        self.assertEqual(result["monitoredSeasons"], [1])
        self.assertEqual(result["profileUsed"], "Sonarr Normal Profile")
        post = session.requests[-1]
        self.assertEqual(post["json"]["qualityProfileId"], 601)
        self.assertEqual(post["json"]["rootFolderPath"], "/configured/tv")
        self.assertTrue(post["json"]["monitored"])
        self.assertTrue(post["json"]["seasonFolder"])
        self.assertEqual(post["json"]["tags"], [21, 22])

    def test_request_series_with_one_season_monitors_only_that_season(self) -> None:
        session = FakeSession(
            [
                [],
                [
                    {
                        "title": "My Brilliant Friend",
                        "tvdbId": 354888,
                        "seasons": [
                            {"seasonNumber": 0, "monitored": True},
                            {"seasonNumber": 1, "monitored": False},
                            {"seasonNumber": 2, "monitored": True},
                        ],
                    }
                ],
                {"title": "My Brilliant Friend", "tvdbId": 354888},
            ]
        )
        service = server.MediaRequestService(config(), session=session)

        result = service.request_series(354888, seasons=[1])

        self.assertEqual(result["status"], "added")
        self.assertEqual(result["monitoredSeasons"], [1])
        self.assertEqual(
            session.requests[-1]["json"]["seasons"],
            [
                {"seasonNumber": 0, "monitored": False},
                {"seasonNumber": 1, "monitored": True},
                {"seasonNumber": 2, "monitored": False},
            ],
        )

    def test_request_series_with_season_range_monitors_only_requested_seasons(
        self,
    ) -> None:
        session = FakeSession(
            [
                [],
                [
                    {
                        "title": "My Brilliant Friend",
                        "tvdbId": 354888,
                        "seasons": [
                            {"seasonNumber": 0},
                            {"seasonNumber": 1},
                            {"seasonNumber": 2},
                            {"seasonNumber": 3},
                        ],
                    }
                ],
                {"title": "My Brilliant Friend", "tvdbId": 354888},
            ]
        )
        service = server.MediaRequestService(config(), session=session)

        result = service.request_series(354888, seasons=[2, 1])

        self.assertEqual(result["monitoredSeasons"], [1, 2])
        self.assertIn("seasons 1-2 monitored", result["message"])
        self.assertEqual(
            [season["monitored"] for season in session.requests[-1]["json"]["seasons"]],
            [False, True, True, False],
        )

    def test_request_series_keeps_specials_unmonitored_unless_requested(self) -> None:
        session = FakeSession(
            [
                [],
                [
                    {
                        "title": "Show With Specials",
                        "tvdbId": 123,
                        "seasons": [{"seasonNumber": 0}, {"seasonNumber": 1}],
                    }
                ],
                {"title": "Show With Specials", "tvdbId": 123},
            ]
        )
        service = server.MediaRequestService(config(), session=session)

        service.request_series(123, seasons=[0])

        self.assertEqual(
            session.requests[-1]["json"]["seasons"],
            [
                {"seasonNumber": 0, "monitored": True},
                {"seasonNumber": 1, "monitored": False},
            ],
        )

    def test_request_series_rejects_nonexistent_requested_season(self) -> None:
        session = FakeSession(
            [
                [],
                [
                    {
                        "title": "Short Show",
                        "tvdbId": 123,
                        "seasons": [{"seasonNumber": 0}, {"seasonNumber": 1}],
                    }
                ],
            ]
        )
        service = server.MediaRequestService(config(), session=session)

        result = service.request_series(123, seasons=[3])

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["monitoredSeasons"], [3])
        self.assertIn("Requested seasons are not available: 3", result["message"])
        self.assertIn("Available seasons: 0, 1", result["message"])
        self.assertEqual(len(session.requests), 2)

    def test_request_series_rejects_invalid_season_values(self) -> None:
        service = server.MediaRequestService(config(), session=FakeSession([]))

        result = service.request_series(123, seasons=[-1])

        self.assertEqual(result["status"], "error")
        self.assertIn("seasons", result["message"])

    def test_request_series_with_seasons_still_enforces_configured_policy(self) -> None:
        session = FakeSession(
            [
                [],
                [
                    {
                        "title": "Policy Show",
                        "tvdbId": 123,
                        "seasons": [{"seasonNumber": 1}],
                    }
                ],
                {"title": "Policy Show", "tvdbId": 123},
            ]
        )
        service = server.MediaRequestService(config(), session=session)

        service.request_series(123, anime=True, seasons=[1])

        post = session.requests[-1]
        self.assertEqual(post["json"]["qualityProfileId"], 602)
        self.assertEqual(post["json"]["rootFolderPath"], "/configured/tv")
        self.assertTrue(post["json"]["monitored"])
        self.assertTrue(post["json"]["seasonFolder"])
        self.assertEqual(post["json"]["tags"], [21, 22])

    def test_request_series_enforces_anime_profile(self) -> None:
        session = FakeSession(
            [
                [],
                [
                    {
                        "title": "Cowboy Bebop",
                        "tvdbId": 76885,
                        "seasons": [{"seasonNumber": 1}],
                    }
                ],
                {"title": "Cowboy Bebop", "tvdbId": 76885},
            ]
        )
        service = server.MediaRequestService(config(), session=session)

        result = service.request_series(76885, anime=True, seasons=[1])

        self.assertEqual(result["profileUsed"], "Sonarr Anime Profile")
        self.assertEqual(session.requests[-1]["json"]["qualityProfileId"], 602)


class RepairBlockedImportsTests(unittest.TestCase):
    def test_repair_blocked_imports_imports_single_safe_sonarr_candidate(self) -> None:
        session = FakeSession(
            [
                {
                    "records": [
                        {
                            "seriesId": 140,
                            "episodeId": 9396,
                            "seasonNumber": 4,
                            "title": "The.Cleaning.Lady.US.S04E02.1080p.WEB-DL-FLUX",
                            "status": "completed",
                            "trackedDownloadState": "importBlocked",
                            "trackedDownloadStatus": "warning",
                            "downloadId": "SABnzbd_nzo_abc",
                            "series": {"id": 140, "title": "The Cleaning Lady"},
                            "episode": {
                                "id": 9396,
                                "seasonNumber": 4,
                                "episodeNumber": 2,
                                "title": "Le Medicin",
                            },
                            "statusMessages": [
                                {
                                    "messages": [
                                        "Found matching series via grab history, but release was matched to series by ID. Automatic import is not possible."
                                    ]
                                }
                            ],
                        }
                    ]
                },
                [
                    {
                        "path": "/data/usenet/complete/tv/The.Cleaning.Lady.S04E02/The.Cleaning.Lady.S04E02.mkv",
                        "folderName": "The.Cleaning.Lady.S04E02",
                        "series": {"id": 140, "title": "The Cleaning Lady"},
                        "seasonNumber": 4,
                        "episodes": [
                            {"id": 9396, "seasonNumber": 4, "episodeNumber": 2}
                        ],
                        "quality": {"quality": {"id": 3, "name": "WEBDL-1080p"}},
                        "languages": [{"id": 1, "name": "English"}],
                        "releaseGroup": "FLUX",
                        "indexerFlags": 0,
                        "releaseType": "singleEpisode",
                        "rejections": [],
                    }
                ],
                {"id": 9001, "status": "queued"},
                [
                    {"id": 9395, "episodeNumber": 1, "hasFile": True},
                    {"id": 9396, "episodeNumber": 2, "hasFile": True},
                ],
            ]
        )
        service = server.MediaRequestService(config(), session=session)

        result = service.repair_blocked_imports(
            query="Cleaning Lady", media_type="series"
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["repairedCount"], 1)
        self.assertEqual(result["items"][0]["status"], "repaired")
        self.assertEqual(result["items"][0]["title"], "The Cleaning Lady")
        command_request = session.requests[2]
        self.assertEqual(command_request["method"], "POST")
        self.assertEqual(command_request["url"], "http://sonarr:8989/api/v3/command")
        self.assertEqual(command_request["json"]["name"], "ManualImport")
        self.assertEqual(command_request["json"]["importMode"], "auto")
        self.assertEqual(
            command_request["json"]["files"][0]["episodeIds"],
            [9396],
        )

    def test_repair_blocked_imports_refuses_ambiguous_sonarr_candidates(self) -> None:
        session = FakeSession(
            [
                {
                    "records": [
                        {
                            "seriesId": 140,
                            "episodeId": 9396,
                            "seasonNumber": 4,
                            "title": "The.Cleaning.Lady.US.S04E02.1080p.WEB-DL-FLUX",
                            "status": "completed",
                            "trackedDownloadState": "importBlocked",
                            "downloadId": "SABnzbd_nzo_abc",
                            "series": {"id": 140, "title": "The Cleaning Lady"},
                            "episode": {"id": 9396, "episodeNumber": 2},
                        }
                    ]
                },
                [
                    {
                        "path": "/downloads/a.mkv",
                        "series": {"id": 140},
                        "episodes": [{"id": 9396}],
                        "rejections": [],
                    },
                    {
                        "path": "/downloads/b.mkv",
                        "series": {"id": 140},
                        "episodes": [{"id": 9396}],
                        "rejections": [],
                    },
                ],
            ]
        )
        service = server.MediaRequestService(config(), session=session)

        result = service.repair_blocked_imports(media_type="series")

        self.assertTrue(result["ok"])
        self.assertEqual(result["repairedCount"], 0)
        self.assertEqual(result["items"][0]["status"], "skipped")
        self.assertIn("2 manual import candidates", result["items"][0]["reason"])
        self.assertEqual(len(session.requests), 2)


class RequestStoreTests(unittest.TestCase):
    def test_request_movie_records_sqlite_row_with_requester(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = server.RequestStore(os.path.join(directory, "requests.sqlite3"))
            session = FakeSession(
                [
                    [],
                    [
                        {
                            "tmdbId": 348,
                            "title": "Alien",
                            "year": 1979,
                            "imdbId": "tt0078748",
                        }
                    ],
                    {
                        "id": 44,
                        "tmdbId": 348,
                        "title": "Alien",
                        "year": 1979,
                        "imdbId": "tt0078748",
                    },
                ]
            )
            service = server.MediaRequestService(
                config(), session=session, request_store=store
            )

            result = service.request_movie(
                348,
                requested_by_user_id=SYNTHETIC_TELEGRAM_ID_1,
                requested_by_chat_id=SYNTHETIC_TELEGRAM_ID_1,
                requested_by_username=SYNTHETIC_REQUESTER_3,
            )

            self.assertEqual(result["status"], "added")
            self.assertEqual(result["requestRecord"], {"recorded": True, "id": 1})
            with sqlite_test_connection(store.db_path) as connection:
                row = connection.execute(
                    """
                    SELECT media_type, title, year, requested_by_user_id,
                           requested_by_chat_id, requested_by_username,
                           radarr_movie_id, tmdb_id, imdb_id, season_numbers,
                           notified_available_at
                    FROM media_requests
                    """
                ).fetchone()
            self.assertEqual(
                row,
                (
                    "movie",
                    "Alien",
                    1979,
                    SYNTHETIC_TELEGRAM_ID_1,
                    SYNTHETIC_TELEGRAM_ID_1,
                    SYNTHETIC_REQUESTER_3,
                    44,
                    348,
                    "tt0078748",
                    None,
                    None,
                ),
            )

    def test_add_request_reuses_same_chat_movie_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = server.RequestStore(os.path.join(directory, "requests.sqlite3"))

            first_id = store.add_request(
                media_type="movie",
                title="The Wizard of the Kremlin",
                year=2026,
                requested_by_user_id=SYNTHETIC_TELEGRAM_ID_4,
                requested_by_chat_id=SYNTHETIC_TELEGRAM_ID_4,
                requested_by_username=SYNTHETIC_REQUESTER_2,
                radarr_movie_id=854,
                tmdb_id=1291659,
            )
            repeated_id = store.add_request(
                media_type="movie",
                title="The Wizard of the Kremlin",
                year=2026,
                requested_by_user_id=SYNTHETIC_TELEGRAM_ID_4,
                requested_by_chat_id=SYNTHETIC_TELEGRAM_ID_4,
                requested_by_username=SYNTHETIC_REQUESTER_2,
                radarr_movie_id=854,
                tmdb_id=1291659,
            )

            other_chat_id = store.add_request(
                media_type="movie",
                title="The Wizard of the Kremlin",
                year=2026,
                requested_by_user_id=SYNTHETIC_TELEGRAM_ID_3,
                requested_by_chat_id=SYNTHETIC_TELEGRAM_ID_3,
                requested_by_username=SYNTHETIC_REQUESTER_1,
                radarr_movie_id=854,
                tmdb_id=1291659,
            )

            self.assertEqual(repeated_id, first_id)
            self.assertNotEqual(other_chat_id, first_id)
            with sqlite_test_connection(store.db_path) as connection:
                rows = connection.execute(
                    """
                    SELECT id, requested_by_chat_id, tmdb_id
                    FROM media_requests
                    ORDER BY id
                    """
                ).fetchall()
            self.assertEqual(
                rows,
                [
                    (first_id, SYNTHETIC_TELEGRAM_ID_4, 1291659),
                    (other_chat_id, SYNTHETIC_TELEGRAM_ID_3, 1291659),
                ],
            )

    def test_request_movie_deduplicates_and_notifies_negative_group_chat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = server.RequestStore(os.path.join(directory, "requests.sqlite3"))
            missing_movie = {
                "id": 854,
                "tmdbId": 1291659,
                "title": "The Wizard of the Kremlin",
                "year": 2026,
                "hasFile": False,
            }
            service = server.MediaRequestService(
                config(),
                session=FakeSession(
                    [
                        [missing_movie],
                        missing_movie,
                        {},
                        [missing_movie],
                        missing_movie,
                        {},
                    ]
                ),
                request_store=store,
            )
            service._with_post_request_status = lambda result, title: result
            group_chat_id = -SYNTHETIC_TELEGRAM_ID_5

            first = service.request_movie(
                1291659,
                requested_by_user_id=SYNTHETIC_TELEGRAM_ID_4,
                requested_by_chat_id=group_chat_id,
                requested_by_username=SYNTHETIC_REQUESTER_2,
            )
            repeated = service.request_movie(
                1291659,
                requested_by_user_id=SYNTHETIC_TELEGRAM_ID_4,
                requested_by_chat_id=group_chat_id,
                requested_by_username=SYNTHETIC_REQUESTER_2,
            )

            self.assertEqual(
                first["requestRecord"]["id"], repeated["requestRecord"]["id"]
            )
            with sqlite_test_connection(store.db_path) as connection:
                rows = connection.execute(
                    "SELECT requested_by_chat_id FROM media_requests"
                ).fetchall()
            self.assertEqual(rows, [(group_chat_id,)])

            sent: list[tuple[int, str]] = []
            service.session = FakeSession([{**missing_movie, "hasFile": True}])
            service.telegram_sender = lambda chat_id, text: (
                sent.append((chat_id, text)) is None
            )
            result = service.notify_movie_available(854)

            self.assertEqual(result["notified"], 1)
            self.assertEqual(sent[0][0], group_chat_id)

    def test_request_movie_reactivates_notified_row_when_file_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = server.RequestStore(os.path.join(directory, "requests.sqlite3"))
            request_id = store.add_request(
                media_type="movie",
                title="Alien",
                year=1979,
                requested_by_user_id=SYNTHETIC_TELEGRAM_ID_3,
                requested_by_chat_id=SYNTHETIC_TELEGRAM_ID_3,
                requested_by_username=SYNTHETIC_REQUESTER_1,
                radarr_movie_id=44,
                tmdb_id=348,
            )
            store.mark_notified(request_id)
            missing_movie = {
                "id": 44,
                "tmdbId": 348,
                "title": "Alien",
                "year": 1979,
                "hasFile": False,
            }
            service = server.MediaRequestService(
                config(),
                session=FakeSession([[missing_movie], missing_movie, {}]),
                request_store=store,
            )

            result = service.request_movie(
                348,
                requested_by_user_id=SYNTHETIC_TELEGRAM_ID_3,
                requested_by_chat_id=SYNTHETIC_TELEGRAM_ID_3,
                requested_by_username=SYNTHETIC_REQUESTER_1,
            )

            self.assertEqual(result["requestRecord"]["id"], request_id)
            with sqlite_test_connection(store.db_path) as connection:
                row = connection.execute(
                    "SELECT status, notified_available_at FROM media_requests WHERE id = ?",
                    (request_id,),
                ).fetchone()
            self.assertEqual(row, ("requested", None))

    def test_initialize_removes_pending_movie_duplicates_before_unique_index(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = os.path.join(directory, "requests.sqlite3")
            store = server.RequestStore(db_path)
            with store._connect() as connection:
                connection.execute("DROP INDEX uq_media_requests_movie_chat_pending")
                for created_at, radarr_movie_id in (
                    (SYNTHETIC_TIMESTAMP_2, 111),
                    (SYNTHETIC_TIMESTAMP_3, 854),
                ):
                    connection.execute(
                        """
                        INSERT INTO media_requests (
                            media_type, title, requested_by_chat_id, tmdb_id,
                            radarr_movie_id, status, created_at, updated_at
                        ) VALUES ('movie', 'The Wizard of the Kremlin', ?, ?, ?,
                                  'requested', ?, ?)
                        """,
                        (
                            SYNTHETIC_TELEGRAM_ID_4,
                            1291659,
                            radarr_movie_id,
                            created_at,
                            created_at,
                        ),
                    )

            server.RequestStore(db_path)

            with sqlite_test_connection(db_path) as connection:
                rows = connection.execute(
                    """
                    SELECT id, radarr_movie_id FROM media_requests
                    WHERE requested_by_chat_id = ? AND tmdb_id = ?
                    ORDER BY id
                    """,
                    (SYNTHETIC_TELEGRAM_ID_4, 1291659),
                ).fetchall()
                indexes = connection.execute(
                    "PRAGMA index_list('media_requests')"
                ).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][1], 854)
            self.assertTrue(
                any(
                    row[1] == "uq_media_requests_movie_chat_pending" and row[2] == 1
                    for row in indexes
                )
            )

    def test_request_series_records_sqlite_row_with_seasons(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = server.RequestStore(os.path.join(directory, "requests.sqlite3"))
            session = FakeSession(
                [
                    [],
                    [
                        {
                            "tvdbId": 82066,
                            "title": "Fringe",
                            "year": 2008,
                            "imdbId": "tt1119644",
                            "tmdbId": 1705,
                            "seasons": [
                                {"seasonNumber": 0},
                                {"seasonNumber": 1},
                                {"seasonNumber": 2},
                            ],
                        }
                    ],
                    {
                        "id": 77,
                        "tvdbId": 82066,
                        "title": "Fringe",
                        "year": 2008,
                        "imdbId": "tt1119644",
                        "tmdbId": 1705,
                        "seasons": [
                            {"seasonNumber": 0},
                            {"seasonNumber": 1},
                            {"seasonNumber": 2},
                        ],
                    },
                ]
            )
            service = server.MediaRequestService(
                config(), session=session, request_store=store
            )

            result = service.request_series(
                82066,
                seasons=[1, 2],
                requested_by_user_id=SYNTHETIC_TELEGRAM_ID_2,
                requested_by_chat_id=SYNTHETIC_TELEGRAM_ID_2,
                requested_by_username=SYNTHETIC_REQUESTER_4,
            )

            self.assertEqual(result["status"], "added")
            self.assertEqual(result["requestRecord"], {"recorded": True, "id": 1})
            with sqlite_test_connection(store.db_path) as connection:
                row = connection.execute(
                    """
                    SELECT media_type, title, year, requested_by_user_id,
                           requested_by_chat_id, requested_by_username,
                           sonarr_series_id, tmdb_id, tvdb_id, imdb_id, season_numbers,
                           notified_available_at
                    FROM media_requests
                    """
                ).fetchone()
            self.assertEqual(
                row,
                (
                    "series",
                    "Fringe",
                    2008,
                    SYNTHETIC_TELEGRAM_ID_2,
                    SYNTHETIC_TELEGRAM_ID_2,
                    SYNTHETIC_REQUESTER_4,
                    77,
                    1705,
                    82066,
                    "tt1119644",
                    "[1, 2]",
                    None,
                ),
            )

    def test_request_store_from_env_defaults_under_hermes_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = server.RequestStore.from_env({"HERMES_HOME": directory})

            self.assertEqual(
                str(store.db_path),
                os.path.join(directory, "state", "media_requests.sqlite3"),
            )
            self.assertTrue(os.path.exists(store.db_path))

    def test_request_store_configures_sqlite_for_shared_container_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = server.RequestStore(os.path.join(directory, "requests.sqlite3"))

            with store._connect() as connection:
                journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
                busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
                foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]

            self.assertEqual(journal_mode.lower(), "wal")
            self.assertEqual(busy_timeout, server.DB_BUSY_TIMEOUT_MS)
            self.assertEqual(foreign_keys, 1)

    def test_notify_available_requests_sends_movie_notifications_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = server.RequestStore(os.path.join(directory, "requests.sqlite3"))
            request_id = store.add_request(
                media_type="movie",
                title="Alien",
                year=1979,
                requested_by_user_id=SYNTHETIC_TELEGRAM_ID_1,
                requested_by_chat_id=SYNTHETIC_TELEGRAM_ID_1,
                requested_by_username=SYNTHETIC_REQUESTER_3,
                radarr_movie_id=44,
                tmdb_id=348,
                imdb_id="tt0078748",
            )
            sent: list[tuple[int, str]] = []
            service = server.MediaRequestService(
                config(),
                session=FakeSession(
                    [{"id": 44, "title": "Alien", "year": 1979, "hasFile": True}]
                ),
                request_store=store,
                telegram_sender=lambda chat_id, text: (
                    sent.append((chat_id, text)) is None
                ),
            )

            result = service.notify_available_requests()

            self.assertEqual(result["notified"], 1)
            self.assertEqual(result["notifications"][0]["id"], request_id)
            self.assertEqual(
                sent,
                [
                    (
                        SYNTHETIC_TELEGRAM_ID_1,
                        "✅ Alien (1979) is now available on Plex.",
                    )
                ],
            )
            with sqlite_test_connection(store.db_path) as connection:
                row = connection.execute(
                    "SELECT status, notified_available_at FROM media_requests WHERE id = ?",
                    (request_id,),
                ).fetchone()
            self.assertEqual(row[0], "available")
            self.assertIsNotNone(row[1])

            result = service.notify_available_requests()
            self.assertEqual(result["checked"], 0)
            self.assertEqual(result["notified"], 0)

    def test_notify_movie_available_claims_row_before_concurrent_send(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = server.RequestStore(os.path.join(directory, "requests.sqlite3"))
            store.add_request(
                media_type="movie",
                title="Alien",
                year=1979,
                requested_by_chat_id=SYNTHETIC_TELEGRAM_ID_3,
                radarr_movie_id=44,
                tmdb_id=348,
            )
            first_send_started = Event()
            release_first_send = Event()
            sent: list[tuple[int, str]] = []

            def send(chat_id: int, text: str) -> bool:
                is_first = not first_send_started.is_set()
                sent.append((chat_id, text))
                if is_first:
                    first_send_started.set()
                    self.assertTrue(release_first_send.wait(timeout=2))
                return True

            service = server.MediaRequestService(
                config(), request_store=store, telegram_sender=send
            )
            service._get_radarr = lambda path: {
                "id": 44,
                "title": "Alien",
                "year": 1979,
                "hasFile": True,
            }

            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(service.notify_movie_available, 44)
                self.assertTrue(first_send_started.wait(timeout=2))
                second = pool.submit(service.notify_movie_available, 44)
                second_result = second.result(timeout=2)
                release_first_send.set()
                first_result = first.result(timeout=2)

            self.assertEqual(len(sent), 1)
            self.assertEqual(first_result["notified"] + second_result["notified"], 1)

    def test_notify_available_requests_waits_until_movie_has_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = server.RequestStore(os.path.join(directory, "requests.sqlite3"))
            store.add_request(
                media_type="movie",
                title="Alien",
                year=1979,
                requested_by_chat_id=SYNTHETIC_TELEGRAM_ID_1,
                radarr_movie_id=44,
                tmdb_id=348,
            )
            sent: list[tuple[int, str]] = []
            service = server.MediaRequestService(
                config(),
                session=FakeSession(
                    [{"id": 44, "title": "Alien", "year": 1979, "hasFile": False}]
                ),
                request_store=store,
                telegram_sender=lambda chat_id, text: (
                    sent.append((chat_id, text)) is None
                ),
            )

            result = service.notify_available_requests()

            self.assertEqual(result["checked"], 1)
            self.assertEqual(result["notified"], 0)
            self.assertEqual(sent, [])
            with sqlite_test_connection(store.db_path) as connection:
                row = connection.execute(
                    "SELECT notified_available_at FROM media_requests"
                ).fetchone()
            self.assertIsNone(row[0])

    def test_notify_available_requests_retries_pending_series(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = server.RequestStore(os.path.join(directory, "requests.sqlite3"))
            request_id = store.add_request(
                media_type="series",
                title="Example Show",
                requested_by_chat_id=SYNTHETIC_TELEGRAM_ID_1,
                sonarr_series_id=77,
                tvdb_id=7001,
                season_numbers=[1],
            )
            sent: list[tuple[int, str]] = []
            service = server.MediaRequestService(
                config(),
                session=FakeSession(
                    [
                        {
                            "id": 77,
                            "title": "Example Show",
                            "seasons": [
                                {
                                    "seasonNumber": 1,
                                    "statistics": {
                                        "episodeFileCount": 8,
                                        "totalEpisodeCount": 8,
                                    },
                                }
                            ],
                        }
                    ]
                ),
                request_store=store,
                telegram_sender=lambda chat_id, text: (
                    sent.append((chat_id, text)) is None
                ),
            )

            result = service.notify_available_requests()

            self.assertTrue(result["ok"])
            self.assertEqual(result["notified"], 1)
            self.assertEqual(result["notifications"][0]["id"], request_id)
            self.assertEqual(len(sent), 1)

    def test_durable_retry_scans_past_the_public_batch_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = server.RequestStore(os.path.join(directory, "requests.sqlite3"))
            for movie_id in range(1, 102):
                store.add_request(
                    media_type="movie",
                    title=f"Example Movie {movie_id}",
                    requested_by_chat_id=SYNTHETIC_TELEGRAM_ID_1,
                    radarr_movie_id=movie_id,
                    tmdb_id=10_000 + movie_id,
                )
            service = server.MediaRequestService(
                config(),
                request_store=store,
                telegram_sender=lambda chat_id, text: True,
            )

            def movie_for_path(path: str) -> dict[str, Any]:
                movie_id = int(path.rsplit("/", 1)[1])
                return {
                    "id": movie_id,
                    "title": f"Example Movie {movie_id}",
                    "hasFile": movie_id == 101,
                }

            service._get_radarr = movie_for_path

            result = service.retry_all_available_requests()

            self.assertEqual(result["checked"], 101)
            self.assertEqual(result["notified"], 1)
            self.assertEqual(result["notifications"][0]["title"], "Example Movie 101")

    def test_public_notification_retry_rejects_invalid_limit(self) -> None:
        service = server.MediaRequestService(config())

        with self.assertRaisesRegex(ValueError, "limit must be a positive integer"):
            service.notify_available_requests(limit=-1)

    def test_notify_movie_available_only_checks_webhook_movie_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = server.RequestStore(os.path.join(directory, "requests.sqlite3"))
            alien_id = store.add_request(
                media_type="movie",
                title="Alien",
                year=1979,
                requested_by_chat_id=SYNTHETIC_TELEGRAM_ID_1,
                radarr_movie_id=44,
                tmdb_id=348,
            )
            store.add_request(
                media_type="movie",
                title="Blade Runner",
                year=1982,
                requested_by_chat_id=SYNTHETIC_TELEGRAM_ID_2,
                radarr_movie_id=55,
                tmdb_id=78,
            )
            sent: list[tuple[int, str]] = []
            service = server.MediaRequestService(
                config(),
                session=FakeSession(
                    [{"id": 44, "title": "Alien", "year": 1979, "hasFile": True}]
                ),
                request_store=store,
                telegram_sender=lambda chat_id, text: (
                    sent.append((chat_id, text)) is None
                ),
            )

            result = service.notify_movie_available(44)

            self.assertEqual(result["checked"], 1)
            self.assertEqual(result["notified"], 1)
            self.assertEqual(result["notifications"][0]["id"], alien_id)
            self.assertEqual(
                sent,
                [
                    (
                        SYNTHETIC_TELEGRAM_ID_1,
                        "✅ Alien (1979) is now available on Plex.",
                    )
                ],
            )
            with sqlite_test_connection(store.db_path) as connection:
                rows = connection.execute(
                    "SELECT title, notified_available_at FROM media_requests ORDER BY id"
                ).fetchall()
            self.assertIsNotNone(rows[0][1])
            self.assertEqual(rows[1][0], "Blade Runner")
            self.assertIsNone(rows[1][1])

    def test_notify_series_available_sends_when_requested_seasons_complete(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = server.RequestStore(os.path.join(directory, "requests.sqlite3"))
            request_id = store.add_request(
                media_type="series",
                title="Fringe",
                year=2008,
                requested_by_chat_id=SYNTHETIC_TELEGRAM_ID_1,
                sonarr_series_id=77,
                tvdb_id=82066,
                season_numbers=[1],
            )
            sent: list[tuple[int, str]] = []
            service = server.MediaRequestService(
                config(),
                session=FakeSession(
                    [
                        {
                            "id": 77,
                            "title": "Fringe",
                            "year": 2008,
                            "seasons": [
                                {
                                    "seasonNumber": 1,
                                    "statistics": {
                                        "episodeFileCount": 20,
                                        "totalEpisodeCount": 20,
                                    },
                                }
                            ],
                        }
                    ]
                ),
                request_store=store,
                telegram_sender=lambda chat_id, text: (
                    sent.append((chat_id, text)) is None
                ),
            )

            result = service.notify_series_available(77)

            self.assertEqual(result["checked"], 1)
            self.assertEqual(result["notified"], 1)
            self.assertEqual(result["notifications"][0]["id"], request_id)
            self.assertEqual(
                sent,
                [
                    (
                        SYNTHETIC_TELEGRAM_ID_1,
                        "✅ Fringe (2008) season 1 is now available on Plex.",
                    )
                ],
            )
            with sqlite_test_connection(store.db_path) as connection:
                row = connection.execute(
                    "SELECT status, notified_available_at FROM media_requests WHERE id = ?",
                    (request_id,),
                ).fetchone()
            self.assertEqual(row[0], "available")
            self.assertIsNotNone(row[1])

    def test_notify_series_available_waits_until_requested_seasons_complete(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = server.RequestStore(os.path.join(directory, "requests.sqlite3"))
            store.add_request(
                media_type="series",
                title="Fringe",
                year=2008,
                requested_by_chat_id=SYNTHETIC_TELEGRAM_ID_1,
                sonarr_series_id=77,
                tvdb_id=82066,
                season_numbers=[1],
            )
            sent: list[tuple[int, str]] = []
            service = server.MediaRequestService(
                config(),
                session=FakeSession(
                    [
                        {
                            "id": 77,
                            "title": "Fringe",
                            "year": 2008,
                            "seasons": [
                                {
                                    "seasonNumber": 1,
                                    "statistics": {
                                        "episodeFileCount": 19,
                                        "totalEpisodeCount": 20,
                                    },
                                }
                            ],
                        }
                    ]
                ),
                request_store=store,
                telegram_sender=lambda chat_id, text: (
                    sent.append((chat_id, text)) is None
                ),
            )

            result = service.notify_series_available(77)

            self.assertEqual(result["checked"], 1)
            self.assertEqual(result["notified"], 0)
            self.assertEqual(sent, [])
            with sqlite_test_connection(store.db_path) as connection:
                row = connection.execute(
                    "SELECT notified_available_at FROM media_requests"
                ).fetchone()
            self.assertIsNone(row[0])


class CorrectnessRegressionTests(unittest.TestCase):
    def test_series_specials_subscription_notifies_when_complete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = server.RequestStore(os.path.join(directory, "requests.sqlite3"))
            store.add_request(
                media_type="series",
                title="Example Show",
                requested_by_chat_id=SYNTHETIC_TELEGRAM_ID_1,
                sonarr_series_id=77,
                tvdb_id=7001,
                season_numbers=[0],
            )
            sent: list[tuple[int, str]] = []
            service = server.MediaRequestService(
                config(),
                session=FakeSession(
                    [
                        {
                            "id": 77,
                            "title": "Example Show",
                            "seasons": [
                                {
                                    "seasonNumber": 0,
                                    "statistics": {
                                        "episodeFileCount": 2,
                                        "totalEpisodeCount": 2,
                                    },
                                }
                            ],
                        }
                    ]
                ),
                request_store=store,
                telegram_sender=lambda chat_id, text: (
                    sent.append((chat_id, text)) is None
                ),
            )

            result = service.notify_series_available(77)

            self.assertTrue(result["ok"])
            self.assertEqual(result["notified"], 1)
            self.assertEqual(
                sent,
                [
                    (
                        SYNTHETIC_TELEGRAM_ID_1,
                        "✅ Example Show specials is now available on Plex.",
                    )
                ],
            )

    def test_series_notification_requires_every_requested_season_to_exist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = server.RequestStore(os.path.join(directory, "requests.sqlite3"))
            store.add_request(
                media_type="series",
                title="Example Show",
                requested_by_chat_id=SYNTHETIC_TELEGRAM_ID_1,
                sonarr_series_id=77,
                tvdb_id=7001,
                season_numbers=[1, 2],
            )
            sent: list[tuple[int, str]] = []
            service = server.MediaRequestService(
                config(),
                session=FakeSession(
                    [
                        {
                            "id": 77,
                            "title": "Example Show",
                            "seasons": [
                                {
                                    "seasonNumber": 1,
                                    "statistics": {
                                        "episodeFileCount": 8,
                                        "totalEpisodeCount": 8,
                                    },
                                }
                            ],
                        }
                    ]
                ),
                request_store=store,
                telegram_sender=lambda chat_id, text: (
                    sent.append((chat_id, text)) is None
                ),
            )

            result = service.notify_series_available(77)

            self.assertTrue(result["ok"])
            self.assertEqual(result["notified"], 0)
            self.assertEqual(
                result["skipped"][0]["availability"]["missingSeasons"], [2]
            )
            self.assertEqual(sent, [])

    def test_series_notification_waits_for_a_requested_season_with_no_episodes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = server.RequestStore(os.path.join(directory, "requests.sqlite3"))
            store.add_request(
                media_type="series",
                title="Example Show",
                requested_by_chat_id=SYNTHETIC_TELEGRAM_ID_1,
                sonarr_series_id=77,
                tvdb_id=7001,
                season_numbers=[1, 2],
            )
            service = server.MediaRequestService(
                config(),
                session=FakeSession(
                    [
                        {
                            "id": 77,
                            "title": "Example Show",
                            "seasons": [
                                {
                                    "seasonNumber": 1,
                                    "statistics": {
                                        "episodeFileCount": 8,
                                        "totalEpisodeCount": 8,
                                    },
                                },
                                {
                                    "seasonNumber": 2,
                                    "statistics": {
                                        "episodeFileCount": 0,
                                        "totalEpisodeCount": 0,
                                    },
                                },
                            ],
                        }
                    ]
                ),
                request_store=store,
                telegram_sender=lambda chat_id, text: True,
            )

            result = service.notify_series_available(77)

            self.assertTrue(result["ok"])
            self.assertEqual(result["notified"], 0)

    def test_pending_series_requests_keep_distinct_season_intent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = server.RequestStore(os.path.join(directory, "requests.sqlite3"))

            def add(seasons: list[int]) -> int:
                return store.add_request(
                    media_type="series",
                    title="Example Show",
                    requested_by_chat_id=SYNTHETIC_TELEGRAM_ID_1,
                    sonarr_series_id=77,
                    tvdb_id=7001,
                    season_numbers=seasons,
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                first_id, second_id = pool.map(add, ([1], [2, 1]))

            self.assertNotEqual(second_id, first_id)
            connection = sqlite3.connect(store.db_path)
            try:
                rows = connection.execute(
                    "SELECT id, season_numbers FROM media_requests"
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual(
                sorted(row[1] for row in rows),
                ["[1, 2]", "[1]"],
            )

    def test_store_migration_merges_legacy_series_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = os.path.join(directory, "requests.sqlite3")
            connection = sqlite3.connect(db_path)
            try:
                connection.executescript(server.RequestStore.SCHEMA)
                for season_numbers in ("[0, 1]", "[1, 0, 1]"):
                    connection.execute(
                        """
                        INSERT INTO media_requests (
                            media_type, title, requested_by_chat_id,
                            sonarr_series_id, tvdb_id, season_numbers,
                            status, created_at, updated_at
                        ) VALUES ('series', 'Example Show', ?, 77, 7001, ?,
                                  'requested', '2026-01-01', '2026-01-01')
                        """,
                        (SYNTHETIC_TELEGRAM_ID_1, season_numbers),
                    )
                connection.commit()
            finally:
                connection.close()

            store = server.RequestStore(db_path)
            connection = sqlite3.connect(store.db_path)
            try:
                rows = connection.execute(
                    "SELECT season_numbers FROM media_requests"
                ).fetchall()
            finally:
                connection.close()

            self.assertEqual(rows, [("[0, 1]",)])

    def test_new_series_aliases_merge_split_exact_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = server.RequestStore(os.path.join(directory, "requests.sqlite3"))
            tvdb_only = store.add_request(
                media_type="series",
                title="Example Show",
                requested_by_chat_id=SYNTHETIC_TELEGRAM_ID_1,
                tvdb_id=7001,
                season_numbers=[1],
            )
            sonarr_only = store.add_request(
                media_type="series",
                title="Example Show",
                requested_by_chat_id=SYNTHETIC_TELEGRAM_ID_1,
                sonarr_series_id=77,
                season_numbers=[1],
            )

            merged = store.add_request(
                media_type="series",
                title="Example Show",
                requested_by_chat_id=SYNTHETIC_TELEGRAM_ID_1,
                sonarr_series_id=77,
                tvdb_id=7001,
                season_numbers=[1],
            )

            self.assertIn(merged, {tvdb_only, sonarr_only})
            connection = sqlite3.connect(store.db_path)
            try:
                rows = connection.execute(
                    "SELECT sonarr_series_id, tvdb_id, season_numbers FROM media_requests"
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual(rows, [(77, 7001, "[1]")])

    def test_new_series_request_is_not_consumed_by_inflight_notification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = server.RequestStore(os.path.join(directory, "requests.sqlite3"))
            first_id = store.add_request(
                media_type="series",
                title="Example Show",
                requested_by_chat_id=SYNTHETIC_TELEGRAM_ID_1,
                sonarr_series_id=77,
                tvdb_id=7001,
                season_numbers=[1],
            )
            original_pending = store.pending_series_notifications_for_sonarr_id
            second_id: int | None = None

            def pending_with_new_request(series_id: int) -> list[dict[str, Any]]:
                nonlocal second_id
                rows = original_pending(series_id)
                second_id = store.add_request(
                    media_type="series",
                    title="Example Show",
                    requested_by_chat_id=SYNTHETIC_TELEGRAM_ID_1,
                    sonarr_series_id=77,
                    tvdb_id=7001,
                    season_numbers=[2],
                )
                return rows

            store.pending_series_notifications_for_sonarr_id = pending_with_new_request
            service = server.MediaRequestService(
                config(),
                session=FakeSession(
                    [
                        {
                            "id": 77,
                            "title": "Example Show",
                            "seasons": [
                                {
                                    "seasonNumber": 1,
                                    "statistics": {
                                        "episodeFileCount": 8,
                                        "totalEpisodeCount": 8,
                                    },
                                }
                            ],
                        }
                    ]
                ),
                request_store=store,
                telegram_sender=lambda chat_id, text: True,
            )

            result = service.notify_series_available(77)

            self.assertEqual(result["notified"], 1)
            self.assertIsNotNone(second_id)
            connection = sqlite3.connect(store.db_path)
            try:
                rows = connection.execute(
                    "SELECT id, status, notified_available_at FROM media_requests ORDER BY id"
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual(rows[0][0:2], (first_id, "available"))
            self.assertIsNotNone(rows[0][2])
            self.assertEqual(rows[1], (second_id, "requested", None))

    def test_request_status_reports_partial_and_complete_series(self) -> None:
        session = FakeSession(
            [
                {"records": []},
                {"records": []},
                [],
                [
                    {
                        "id": 7,
                        "title": "Partial Show",
                        "monitored": True,
                        "seasons": [
                            {
                                "seasonNumber": 1,
                                "monitored": True,
                                "statistics": {
                                    "episodeFileCount": 2,
                                    "totalEpisodeCount": 3,
                                },
                            }
                        ],
                    },
                    {
                        "id": 8,
                        "title": "Complete Show",
                        "monitored": True,
                        "seasons": [
                            {
                                "seasonNumber": 1,
                                "monitored": True,
                                "statistics": {
                                    "episodeFileCount": 3,
                                    "totalEpisodeCount": 3,
                                },
                            }
                        ],
                    },
                ],
            ]
        )

        result = server.MediaRequestService(config(), session=session).request_status()

        statuses = {item["title"]: item["status"] for item in result["items"]}
        self.assertEqual(statuses["Partial Show"], "partially_available")
        self.assertEqual(statuses["Complete Show"], "available")

    def test_request_store_connection_is_closed_after_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = server.RequestStore(os.path.join(directory, "requests.sqlite3"))

            with store._connect() as connection:
                self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)

            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")

    def test_telegram_failure_is_retryable_and_does_not_mark_notified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = server.RequestStore(os.path.join(directory, "requests.sqlite3"))
            request_id = store.add_request(
                media_type="movie",
                title="Example Movie",
                requested_by_chat_id=SYNTHETIC_TELEGRAM_ID_1,
                radarr_movie_id=44,
                tmdb_id=4001,
            )
            service = server.MediaRequestService(
                config(),
                session=FakeSession(
                    [{"id": 44, "title": "Example Movie", "hasFile": True}]
                ),
                request_store=store,
                telegram_sender=lambda chat_id, text: False,
            )

            result = service.notify_movie_available(44)

            self.assertFalse(result["ok"])
            self.assertEqual(result["notified"], 0)
            connection = sqlite3.connect(store.db_path)
            try:
                row = connection.execute(
                    "SELECT status, notified_available_at FROM media_requests WHERE id = ?",
                    (request_id,),
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(row, ("requested", None))

    def test_already_available_movie_does_not_create_pending_notification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = server.RequestStore(os.path.join(directory, "requests.sqlite3"))
            service = server.MediaRequestService(
                config(),
                session=FakeSession(
                    [
                        [
                            {
                                "id": 44,
                                "title": "Example Movie",
                                "tmdbId": 4001,
                                "hasFile": True,
                            }
                        ]
                    ]
                ),
                request_store=store,
            )

            result = service.request_movie(
                4001,
                requested_by_user_id=SYNTHETIC_TELEGRAM_ID_2,
                requested_by_chat_id=SYNTHETIC_TELEGRAM_ID_1,
            )

            self.assertEqual(result["status"], "already_exists")
            with sqlite_test_connection(store.db_path) as connection:
                row = connection.execute(
                    "SELECT status, notified_available_at FROM media_requests"
                ).fetchone()
            self.assertEqual(row[0], "available")
            self.assertIsNotNone(row[1])
            self.assertEqual(store.pending_movie_notifications(), [])

    def test_already_complete_series_does_not_create_pending_notification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = server.RequestStore(os.path.join(directory, "requests.sqlite3"))
            existing = {
                "id": 77,
                "title": "Example Show",
                "tvdbId": 7001,
                "seasons": [
                    {
                        "seasonNumber": 1,
                        "monitored": True,
                        "statistics": {
                            "episodeFileCount": 8,
                            "totalEpisodeCount": 8,
                        },
                    }
                ],
            }
            service = server.MediaRequestService(
                config(),
                session=FakeSession([[existing], existing, {"id": 99}]),
                request_store=store,
            )

            result = service.request_series(
                7001,
                seasons=[1],
                requested_by_user_id=SYNTHETIC_TELEGRAM_ID_2,
                requested_by_chat_id=SYNTHETIC_TELEGRAM_ID_1,
            )

            self.assertEqual(result["status"], "already_exists")
            with sqlite_test_connection(store.db_path) as connection:
                row = connection.execute(
                    "SELECT status, notified_available_at FROM media_requests"
                ).fetchone()
            self.assertEqual(row[0], "available")
            self.assertIsNotNone(row[1])
            self.assertEqual(store.pending_series_notifications(), [])

    def test_movie_add_conflict_refetches_and_records_requester(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = server.RequestStore(os.path.join(directory, "requests.sqlite3"))
            service = server.MediaRequestService(config(), request_store=store)
            existing_calls = iter(
                [None, {"id": 44, "title": "Example Movie", "tmdbId": 4001}]
            )
            service._find_existing_movie = lambda tmdb_id: next(existing_calls)
            service._lookup_movie_by_tmdb = lambda tmdb_id: {
                "title": "Example Movie",
                "tmdbId": tmdb_id,
            }

            def fail_add(path: str, json: dict[str, Any]) -> dict[str, Any]:
                if path == "/api/v3/movie":
                    raise server.ArrApiError("duplicate", status_code=409)
                return {}

            service._post_radarr = fail_add
            service._put_radarr = lambda path, json: dict(json)
            service._with_post_request_status = lambda result, title: result

            result = service.request_movie(
                4001,
                requested_by_user_id=SYNTHETIC_TELEGRAM_ID_2,
                requested_by_chat_id=SYNTHETIC_TELEGRAM_ID_1,
            )

            self.assertEqual(result["status"], "already_exists")
            self.assertTrue(result["requestRecord"]["recorded"])

    def test_series_add_conflict_refetches_and_records_requested_seasons(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = server.RequestStore(os.path.join(directory, "requests.sqlite3"))
            service = server.MediaRequestService(config(), request_store=store)
            series = {
                "title": "Example Show",
                "tvdbId": 7001,
                "seasons": [
                    {"seasonNumber": 1, "monitored": False},
                    {"seasonNumber": 2, "monitored": False},
                ],
            }
            existing_calls = iter([None, {**series, "id": 77}])
            service._find_existing_show = lambda tvdb_id: next(existing_calls)
            service._lookup_show_by_tvdb = lambda tvdb_id: series

            def post(path: str, json: dict[str, Any]) -> dict[str, Any]:
                if path == "/api/v3/series":
                    raise server.ArrApiError("duplicate", status_code=409)
                return {}

            service._post_sonarr = post
            service._put_sonarr = lambda path, json: json
            service._with_post_request_status = lambda result, title: result

            result = service.request_series(
                7001,
                seasons=[2],
                requested_by_user_id=SYNTHETIC_TELEGRAM_ID_2,
                requested_by_chat_id=SYNTHETIC_TELEGRAM_ID_1,
            )

            self.assertEqual(result["status"], "already_exists")
            self.assertTrue(result["requestRecord"]["recorded"])
            connection = sqlite3.connect(store.db_path)
            try:
                row = connection.execute(
                    "SELECT sonarr_series_id, tvdb_id, season_numbers FROM media_requests"
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(row, (77, 7001, "[2]"))

    def test_webhook_retry_response_is_sanitized(self) -> None:
        handler = FakeWebhookHandler(b"", {})

        webhook_bridge._notification_response(
            handler,
            service="radarr",
            identifier_name="radarrMovieId",
            identifier=44,
            result={
                "ok": False,
                "checked": 1,
                "notified": 0,
                "notifications": [
                    {
                        "chatId": SYNTHETIC_TELEGRAM_ID_1,
                        "title": "Private Request Title",
                    }
                ],
                "skipped": [{"reason": "internal detail"}],
            },
        )

        serialized = handler.wfile.getvalue().decode("utf-8")
        self.assertEqual(handler.status, 503)
        self.assertNotIn("chatId", serialized)
        self.assertNotIn("Private Request Title", serialized)
        self.assertNotIn("internal detail", serialized)


class RadarrWebhookBridgeTests(unittest.TestCase):
    def test_default_webhook_port_is_unique(self) -> None:
        self.assertEqual(webhook_bridge.DEFAULT_PORT, 18081)

    def test_load_max_body_bytes_validates_env_value(self) -> None:
        self.assertEqual(
            webhook_bridge._load_max_body_bytes(None),
            webhook_bridge.DEFAULT_MAX_BODY_BYTES,
        )
        self.assertEqual(webhook_bridge._load_max_body_bytes("1024"), 1024)
        with self.assertRaisesRegex(RuntimeError, webhook_bridge.ENV_MAX_BODY_BYTES):
            webhook_bridge._load_max_body_bytes("0")
        with self.assertRaisesRegex(RuntimeError, webhook_bridge.ENV_MAX_BODY_BYTES):
            webhook_bridge._load_max_body_bytes("large")

    def test_load_retry_interval_validates_env_value(self) -> None:
        self.assertEqual(
            webhook_bridge._load_retry_interval_seconds(None),
            webhook_bridge.DEFAULT_RETRY_INTERVAL_SECONDS,
        )
        self.assertEqual(webhook_bridge._load_retry_interval_seconds("0"), 0)
        self.assertEqual(webhook_bridge._load_retry_interval_seconds("30"), 30)
        with self.assertRaisesRegex(
            RuntimeError, webhook_bridge.ENV_RETRY_INTERVAL_SECONDS
        ):
            webhook_bridge._load_retry_interval_seconds("-1")
        with self.assertRaisesRegex(
            RuntimeError, webhook_bridge.ENV_RETRY_INTERVAL_SECONDS
        ):
            webhook_bridge._load_retry_interval_seconds("soon")

    def test_retry_worker_runs_pending_backfill(self) -> None:
        stop_event = Event()

        class FakeRetryService:
            calls = 0

            def retry_all_available_requests(self) -> dict[str, Any]:
                self.calls += 1
                stop_event.set()
                return {"ok": True, "checked": 0, "notified": 0}

        service = FakeRetryService()

        webhook_bridge.retry_pending_notifications(service, stop_event, 0.001)

        self.assertEqual(service.calls, 1)

    def test_webhook_payload_accepts_json_content_type(self) -> None:
        body = b'{"eventType": "Download"}'
        handler = FakeWebhookHandler(
            body,
            {
                "Content-Type": "application/json; charset=utf-8",
                "Content-Length": str(len(body)),
            },
        )

        payload = webhook_bridge.ArrWebhookHandler._read_payload(handler)

        self.assertEqual(payload, {"eventType": "Download"})
        self.assertIsNone(handler.status)

    def test_webhook_payload_rejects_non_json_content_type(self) -> None:
        body = b'{"eventType": "Download"}'
        handler = FakeWebhookHandler(
            body,
            {
                "Content-Type": "text/plain",
                "Content-Length": str(len(body)),
            },
        )

        payload = webhook_bridge.ArrWebhookHandler._read_payload(handler)

        self.assertIsNone(payload)
        self.assertEqual(handler.status, 415)
        self.assertEqual(
            handler.response_json()["error"],
            "content type must be application/json",
        )

    def test_webhook_payload_rejects_invalid_content_length(self) -> None:
        handler = FakeWebhookHandler(
            b"{}",
            {"Content-Type": "application/json", "Content-Length": "not-a-number"},
        )

        payload = webhook_bridge.ArrWebhookHandler._read_payload(handler)

        self.assertIsNone(payload)
        self.assertEqual(handler.status, 400)
        self.assertEqual(handler.response_json()["error"], "invalid Content-Length")

    def test_webhook_payload_rejects_oversized_body_before_reading(self) -> None:
        handler = FakeWebhookHandler(
            b"{}",
            {"Content-Type": "application/json", "Content-Length": "3"},
            max_body_bytes=2,
        )

        payload = webhook_bridge.ArrWebhookHandler._read_payload(handler)

        self.assertIsNone(payload)
        self.assertEqual(handler.status, 413)
        self.assertEqual(handler.response_json()["error"], "request body too large")

    def test_webhook_payload_sanitizes_invalid_json_errors(self) -> None:
        handler = FakeWebhookHandler(
            b"{",
            {"Content-Type": "application/json", "Content-Length": "1"},
        )

        payload = webhook_bridge.ArrWebhookHandler._read_payload(handler)

        self.assertIsNone(payload)
        self.assertEqual(handler.status, 400)
        self.assertEqual(handler.response_json()["error"], "invalid JSON body")

    def test_extract_radarr_movie_id_accepts_positive_ints(self) -> None:
        self.assertEqual(
            webhook_bridge.extract_radarr_movie_id({"movie": {"id": 44}}), 44
        )
        self.assertEqual(
            webhook_bridge.extract_radarr_movie_id({"movie": {"id": "55"}}), 55
        )

    def test_extract_radarr_movie_id_rejects_missing_or_invalid_values(self) -> None:
        self.assertIsNone(webhook_bridge.extract_radarr_movie_id({}))
        self.assertIsNone(webhook_bridge.extract_radarr_movie_id({"movie": None}))
        self.assertIsNone(webhook_bridge.extract_radarr_movie_id({"movie": {"id": 0}}))
        self.assertIsNone(
            webhook_bridge.extract_radarr_movie_id({"movie": {"id": True}})
        )
        self.assertIsNone(
            webhook_bridge.extract_radarr_movie_id({"movie": {"id": "abc"}})
        )

    def test_extract_sonarr_series_id_accepts_positive_ints(self) -> None:
        self.assertEqual(
            webhook_bridge.extract_sonarr_series_id({"series": {"id": 77}}), 77
        )
        self.assertEqual(
            webhook_bridge.extract_sonarr_series_id({"series": {"id": "88"}}), 88
        )

    def test_extract_sonarr_series_id_rejects_missing_or_invalid_values(self) -> None:
        self.assertIsNone(webhook_bridge.extract_sonarr_series_id({}))
        self.assertIsNone(webhook_bridge.extract_sonarr_series_id({"series": None}))
        self.assertIsNone(
            webhook_bridge.extract_sonarr_series_id({"series": {"id": 0}})
        )
        self.assertIsNone(
            webhook_bridge.extract_sonarr_series_id({"series": {"id": False}})
        )
        self.assertIsNone(
            webhook_bridge.extract_sonarr_series_id({"series": {"id": "abc"}})
        )

    def test_should_handle_only_file_available_radarr_events(self) -> None:
        self.assertTrue(
            webhook_bridge.should_handle_radarr_event({"eventType": "Download"})
        )
        self.assertTrue(
            webhook_bridge.should_handle_radarr_event({"eventType": "Rename"})
        )
        self.assertTrue(
            webhook_bridge.should_handle_radarr_event({"eventType": "MovieFileUpgrade"})
        )
        self.assertFalse(
            webhook_bridge.should_handle_radarr_event({"eventType": "Grab"})
        )
        self.assertFalse(
            webhook_bridge.should_handle_radarr_event({"eventType": "MovieDelete"})
        )

    def test_should_handle_only_file_available_sonarr_events(self) -> None:
        self.assertTrue(
            webhook_bridge.should_handle_sonarr_event({"eventType": "Download"})
        )
        self.assertTrue(
            webhook_bridge.should_handle_sonarr_event(
                {"eventType": "EpisodeFileUpgrade"}
            )
        )
        self.assertFalse(
            webhook_bridge.should_handle_sonarr_event({"eventType": "Grab"})
        )
        self.assertFalse(
            webhook_bridge.should_handle_sonarr_event({"eventType": "SeriesDelete"})
        )


if __name__ == "__main__":
    unittest.main()
