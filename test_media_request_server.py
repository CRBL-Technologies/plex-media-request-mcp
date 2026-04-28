from __future__ import annotations

import json
import os
import sys
import types
import unittest
from unittest.mock import patch
from typing import Any

import media_request_server as server


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
        with self.assertRaisesRegex(
            RuntimeError, server.ENV_RADARR_QUALITY_PROFILE_ID
        ):
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


class SearchTests(unittest.TestCase):
    def test_search_movie_returns_normalized_results(self) -> None:
        session = FakeSession(
            [
                [
                    {
                        "title": "Dune",
                        "year": 2021,
                        "tmdbId": 438631,
                        "imdbId": "tt1160419",
                        "runtime": 155,
                        "overview": "A gifted young man travels to Arrakis.",
                        "alreadyExists": False,
                        "images": [
                            {
                                "coverType": "poster",
                                "url": "http://radarr:7878/MediaCover/1/poster.jpg",
                                "remoteUrl": "https://image.tmdb.org/poster.jpg",
                            }
                        ],
                        "rootFolderPath": "/hidden",
                    }
                ]
            ]
        )
        service = server.MediaRequestService(config(), session=session)

        results = service.search_movie("alien")

        self.assertEqual(
            results,
            [
                {
                    "title": "Dune",
                    "year": 2021,
                    "tmdb_id": 438631,
                    "imdb_id": "tt1160419",
                    "runtime_minutes": 155,
                    "overview": "A gifted young man travels to Arrakis.",
                    "poster_url": "https://image.tmdb.org/poster.jpg",
                    "in_library": False,
                    "already_exists": False,
                }
            ],
        )

    def test_search_movie_defaults_to_five_results(self) -> None:
        session = FakeSession(
            [
                [
                    {"title": f"Movie {index}", "year": 2000 + index}
                    for index in range(6)
                ]
            ]
        )
        service = server.MediaRequestService(config(), session=session)

        results = service.search_movie("movie")

        self.assertEqual(len(results), 5)

    def test_search_movie_clamps_limit_to_ten_results(self) -> None:
        session = FakeSession(
            [
                [
                    {"title": f"Movie {index}", "year": 2000 + index}
                    for index in range(12)
                ]
            ]
        )
        service = server.MediaRequestService(config(), session=session)

        results = service.search_movie("movie", limit=99)

        self.assertEqual(len(results), 10)

    def test_search_movie_rejects_invalid_limit(self) -> None:
        service = server.MediaRequestService(config(), session=FakeSession([]))

        with self.assertRaisesRegex(ValueError, "limit"):
            service.search_movie("movie", limit=0)

    def test_search_show_returns_normalized_results(self) -> None:
        session = FakeSession(
            [
                [
                    {
                        "title": "Cowboy Bebop",
                        "year": 1998,
                        "tvdbId": 76885,
                        "imdbId": "tt0213338",
                        "tmdbId": 30991,
                        "seasons": [{"seasonNumber": 1}, {"seasonNumber": 0}],
                        "status": "ended",
                        "overview": "Bounty hunters drift through space.",
                        "images": [
                            {
                                "coverType": "poster",
                                "remoteUrl": "https://art.example/poster.jpg",
                            }
                        ],
                        "genres": ["Anime"],
                        "alreadyExists": True,
                    }
                ]
            ]
        )
        service = server.MediaRequestService(config(), session=session)

        results = service.search_show("bebop")

        self.assertEqual(
            results,
            [
                {
                    "title": "Cowboy Bebop",
                    "year": 1998,
                    "tvdb_id": 76885,
                    "imdb_id": "tt0213338",
                    "tmdb_id": 30991,
                    "season_count": 2,
                    "status": "ended",
                    "overview": "Bounty hunters drift through space.",
                    "poster_url": "https://art.example/poster.jpg",
                    "is_anime": True,
                    "in_library": True,
                    "already_exists": True,
                }
            ],
        )

    def test_search_show_respects_limit(self) -> None:
        session = FakeSession(
            [
                [
                    {"title": f"Show {index}", "year": 2000 + index}
                    for index in range(4)
                ]
            ]
        )
        service = server.MediaRequestService(config(), session=session)

        results = service.search_show("show", limit=2)

        self.assertEqual(len(results), 2)

    def test_search_show_omits_unavailable_optional_fields(self) -> None:
        session = FakeSession([[{"title": "Unknown", "year": 2024}]])
        service = server.MediaRequestService(config(), session=session)

        results = service.search_show("unknown")

        self.assertEqual(results, [{"title": "Unknown", "year": 2024, "tvdb_id": None}])


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
            [(request["method"], request["url"]) for request in session.requests],
            [
                ("GET", "http://radarr:7878/api/v3/queue"),
                ("GET", "http://sonarr:8989/api/v3/queue"),
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
                        "physicalRelease": "2000-01-01T00:00:00Z",
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
                        "physicalRelease": "2000-01-01T00:00:00Z",
                    },
                    {
                        "id": 43,
                        "title": "Alien",
                        "monitored": True,
                        "hasFile": False,
                        "physicalRelease": "2000-01-01T00:00:00Z",
                    },
                ],
                [],
            ]
        )
        service = server.MediaRequestService(config(), session=session)

        result = service.request_status(query="alien")

        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["title"], "Alien")


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

        with patch.dict(
            sys.modules,
            {
                "mcp": mcp_module,
                "mcp.server": server_module,
                "mcp.server.fastmcp": fastmcp_module,
            },
        ), patch.dict(os.environ, env_config(), clear=True):
            mcp = server.create_server()

        self.assertEqual(
            mcp.tools,
            [
                "search_movie",
                "add_movie",
                "search_show",
                "add_show",
                "media_status",
                "download_status",
                "request_status",
            ],
        )


class AddTests(unittest.TestCase):
    def test_add_movie_enforces_configured_radarr_policy(self) -> None:
        session = FakeSession(
            [
                [],
                [{"title": "Alien", "tmdbId": 348, "titleSlug": "alien-1979"}],
                {"title": "Alien", "tmdbId": 348},
            ]
        )
        service = server.MediaRequestService(config(), session=session)

        result = service.add_movie(348)

        self.assertEqual(result["status"], "added")
        post = session.requests[-1]
        self.assertEqual(post["method"], "POST")
        self.assertEqual(post["json"]["qualityProfileId"], 501)
        self.assertEqual(post["json"]["rootFolderPath"], "/configured/movies")
        self.assertTrue(post["json"]["monitored"])
        self.assertEqual(post["json"]["minimumAvailability"], "announced")
        self.assertEqual(post["json"]["tags"], [11])

    def test_add_movie_reports_existing(self) -> None:
        session = FakeSession([[{"title": "Alien", "tmdbId": 348}]])
        service = server.MediaRequestService(config(), session=session)

        result = service.add_movie(348)

        self.assertEqual(result["status"], "already_exists")
        self.assertEqual(len(session.requests), 1)

    def test_add_show_enforces_normal_profile(self) -> None:
        session = FakeSession(
            [
                [],
                [{"title": "Fringe", "tvdbId": 82066, "titleSlug": "fringe"}],
                {"title": "Fringe", "tvdbId": 82066},
            ]
        )
        service = server.MediaRequestService(config(), session=session)

        result = service.add_show(82066)

        self.assertEqual(result["profileUsed"], "Sonarr Normal Profile")
        post = session.requests[-1]
        self.assertEqual(post["json"]["qualityProfileId"], 601)
        self.assertEqual(post["json"]["rootFolderPath"], "/configured/tv")
        self.assertTrue(post["json"]["monitored"])
        self.assertTrue(post["json"]["seasonFolder"])
        self.assertEqual(post["json"]["tags"], [21, 22])

    def test_add_show_enforces_anime_profile(self) -> None:
        session = FakeSession(
            [
                [],
                [{"title": "Cowboy Bebop", "tvdbId": 76885}],
                {"title": "Cowboy Bebop", "tvdbId": 76885},
            ]
        )
        service = server.MediaRequestService(config(), session=session)

        result = service.add_show(76885, anime=True)

        self.assertEqual(result["profileUsed"], "Sonarr Anime Profile")
        self.assertEqual(session.requests[-1]["json"]["qualityProfileId"], 602)


if __name__ == "__main__":
    unittest.main()
