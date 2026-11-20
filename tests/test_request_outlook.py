"""A request answers at once with what to expect, then follows up once."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from conftest import FakeUpstream
from starlette.testclient import TestClient

from media_gateway import tools
from media_gateway.app import create_app
from media_gateway.config import Config
from media_gateway.store import Store
from media_gateway.tools import ToolService
from media_gateway.types import Actor, Role

ACTOR = Actor.from_json({"user_id": 1001, "chat_id": 555, "first_name": "Ada"})
MOVIE = {"id": 7, "tmdbId": 123, "title": "A <Movie>", "year": 2026, "isAvailable": True}
SERIES = {"id": 9, "tvdbId": 456, "title": "A Show", "year": 2026, "seasons": [{"seasonNumber": 1}]}


class Harness:
    def __init__(self, config: Config) -> None:
        self.fake = FakeUpstream()
        self.sent: list[tuple[int, str]] = []
        self.service = ToolService(config, Store(config.db_path), self.fake, self._notify)  # type: ignore[arg-type]

    async def _notify(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))

    async def request(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self.service.call(name, arguments, ACTOR, Role.USER)
        await asyncio.gather(*self.service._follow_ups)
        return result


@pytest.fixture
def harness(config: Config, monkeypatch: pytest.MonkeyPatch) -> Harness:
    monkeypatch.setattr(tools, "FOLLOW_UP_POLL_SECONDS", 0)
    return Harness(config)


def _movie(harness: Harness, **fields: Any) -> None:
    harness.fake.responses["radarr_search_movie"] = {"data": [{**MOVIE, **fields}]}
    harness.fake.responses["radarr_get_movie"] = {"id": 7, "hasFile": False}


def _series(harness: Harness, episodes: list[dict[str, Any]]) -> None:
    harness.fake.responses["sonarr_search_series"] = {"data": [SERIES]}
    harness.fake.responses["sonarr_get_series_by_id"] = {
        "id": 9,
        "seasons": [{"seasonNumber": 1, "monitored": False}],
    }
    harness.fake.responses["sonarr_get_episodes"] = {"data": episodes}


def _episode(number: int, air_date: str, *, held: bool = False) -> dict[str, Any]:
    return {
        "id": number,
        "seasonNumber": 1,
        "episodeNumber": number,
        "airDateUtc": air_date,
        "hasFile": held,
    }


@pytest.mark.follow_up
async def test_an_unreleased_movie_says_when_it_is_out_and_sends_no_follow_up(
    harness: Harness,
) -> None:
    _movie(harness, isAvailable=False, inCinemas="2026-12-16T00:00:00Z")

    result = await harness.request("request_movie", {"tmdb_id": 123})

    assert result["status"] == "search_started"
    assert result["outlook"] == {"state": "not_released", "in_cinemas": "2026-12-16"}
    assert harness.sent == []
    assert not any(name == "radarr_queue" for name, _ in harness.fake.calls)


@pytest.mark.follow_up
async def test_a_released_movie_answers_at_once_then_reports_the_download(
    harness: Harness,
) -> None:
    _movie(harness)
    queues = [[], [], [{"movieId": 8}, {"movieId": 7, "timeleft": "00:09:26"}]]
    harness.fake.responses["radarr_queue"] = lambda _args: queues.pop(0)

    result = await harness.request("request_movie", {"tmdb_id": 123})

    # The answer does not wait for the download: it was not there yet.
    assert result["outlook"] == {"state": "searching"}
    assert harness.sent == [
        (555, "⬇️ <b>Download started</b>\nA &lt;Movie&gt; (2026)\nAbout 9 min left")
    ]


@pytest.mark.follow_up
async def test_a_search_that_finds_nothing_says_so_once(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tools, "FOLLOW_UP_SECONDS", 0.05)
    _movie(harness)

    result = await harness.request("request_movie", {"tmdb_id": 123})

    assert result["outlook"] == {"state": "searching"}
    assert harness.sent == [
        (
            555,
            "🔎 <b>No download yet</b>\nA &lt;Movie&gt; (2026)\n"
            "It is out, but no release was found. The search continues.",
        )
    ]


@pytest.mark.follow_up
async def test_a_season_that_has_not_aired_gives_its_date(harness: Harness) -> None:
    _series(harness, [_episode(1, "2999-01-02T09:00:00Z"), _episode(2, "2999-01-09T09:00:00Z")])

    result = await harness.request("request_series", {"tvdb_id": 456, "seasons": [1]})

    assert result["outlook"] == {"state": "not_aired", "next_episode_airs": "2999-01-02"}
    assert harness.sent == []


@pytest.mark.follow_up
async def test_an_airing_season_reports_its_download_and_the_next_episode(
    harness: Harness,
) -> None:
    _series(harness, [_episode(1, "2020-01-02T09:00:00Z"), _episode(2, "2999-01-09T09:00:00Z")])
    queues = [
        [],
        [
            {"seriesId": 9, "seasonNumber": 2, "timeleft": "09:00:00"},
            {"seriesId": 9, "seasonNumber": 1, "timeleft": "01:05:00"},
        ],
    ]
    harness.fake.responses["sonarr_get_queue"] = lambda _args: {"data": queues.pop(0)}

    result = await harness.request("request_series", {"tvdb_id": 456, "seasons": [1]})

    assert result["outlook"] == {"state": "searching", "next_episode_airs": "2999-01-09"}
    assert harness.sent == [
        (555, "⬇️ <b>Download started</b>\nA Show · Season 1\nAbout 1 h 05 min left")
    ]


@pytest.mark.follow_up
async def test_a_new_series_without_episodes_yet_is_sorted_out_by_the_follow_up(
    harness: Harness,
) -> None:
    _series(harness, [])
    reads = [[], [_episode(1, "2999-01-02T09:00:00Z")]]
    harness.fake.responses["sonarr_get_episodes"] = lambda args: (
        {"data": reads.pop(0)} if "seasonNumber" not in args else {"data": []}
    )

    result = await harness.request("request_series", {"tvdb_id": 456, "seasons": [1]})

    assert result["outlook"] == {"state": "searching"}
    assert harness.sent == [
        (555, "🗓 <b>Not out yet</b>\nA Show · Season 1\nNext episode: 2999-01-02")
    ]


@pytest.mark.follow_up
async def test_a_failed_outlook_does_not_fail_the_request(harness: Harness) -> None:
    _movie(harness)
    harness.fake.responses["radarr_queue"] = RuntimeError("queue is down")

    result = await harness.request("request_movie", {"tmdb_id": 123})

    assert result["status"] == "search_started"
    assert "outlook" not in result
    assert harness.sent == []


@pytest.mark.follow_up
async def test_a_bulk_request_starts_no_follow_up(harness: Harness) -> None:
    _movie(harness)

    result = await harness.request("request_titles", {"titles": ["A <Movie>"]})

    assert result["counts"]["requested"] == 1
    assert "outlook" not in result["requested"][0]
    assert harness.sent == []
    assert not any(name == "radarr_queue" for name, _ in harness.fake.calls)


async def test_the_follow_up_reaches_telegram_as_a_plain_message(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    posts: list[tuple[str, dict[str, Any]]] = []

    class RecordingClient:
        async def __aenter__(self) -> RecordingClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, url: str, *, json: dict[str, Any]) -> httpx.Response:
            posts.append((url.rsplit("/", 1)[-1], json))
            return httpx.Response(200, request=httpx.Request("POST", "https://telegram.test"))

    monkeypatch.setattr(
        "media_gateway.notifications.httpx.AsyncClient", lambda **_kwargs: RecordingClient()
    )
    app = create_app(config)
    with TestClient(app):
        await app.state.runtime.tools.notify(555, "<b>Download started</b>")

    # No Plex button: the title is not in Plex yet.
    assert posts == [
        (
            "sendMessage",
            {
                "chat_id": 555,
                "text": "<b>Download started</b>",
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
    ]
