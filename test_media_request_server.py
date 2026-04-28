from __future__ import annotations

import unittest
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
    def test_search_movie_returns_concise_results(self) -> None:
        session = FakeSession(
            [
                [
                    {
                        "title": "Alien",
                        "year": 1979,
                        "tmdbId": 348,
                        "overview": "Space horror.",
                        "isExisting": False,
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
                    "title": "Alien",
                    "year": 1979,
                    "tmdbId": 348,
                    "overview": "Space horror.",
                    "isExisting": False,
                }
            ],
        )

    def test_search_show_returns_genres(self) -> None:
        session = FakeSession(
            [
                [
                    {
                        "title": "Cowboy Bebop",
                        "year": 1998,
                        "tvdbId": 76885,
                        "genres": ["Anime"],
                        "alreadyExists": True,
                    }
                ]
            ]
        )
        service = server.MediaRequestService(config(), session=session)

        results = service.search_show("bebop")

        self.assertEqual(results[0]["genres"], ["Anime"])
        self.assertTrue(results[0]["alreadyExists"])


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
