from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import Any, ClassVar

import httpx
import pytest
from conftest import FakeUpstream
from starlette.testclient import TestClient

from media_gateway import plex_watch
from media_gateway.app import COOKIE, create_app
from media_gateway.config import Config
from media_gateway.constants import ADMIN_UPSTREAM_TOOLS, SHARED_TOOLS
from media_gateway.dashboard import _request_status
from media_gateway.types import Actor
from media_gateway.upstream import UpstreamError


def _idle_queue() -> FakeUpstream:
    """An upstream whose Sonarr queue is empty: nothing is still downloading."""

    fake = FakeUpstream()
    fake.responses["sonarr_get_queue"] = {"data": {"records": []}}
    return fake


def _actor(user_id: int, **extra: Any) -> dict[str, Any]:
    return {"user_id": user_id, "chat_id": user_id, **extra}


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer gateway-secret-with-at-least-32-bytes"}


def _login(client: TestClient) -> str:
    response = client.post(
        "/login",
        data={"password": "correct horse battery staple"},
        headers={"Origin": "http://nas.lan:18082", "User-Agent": "Firefox"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert client.cookies.get(COOKIE)
    return str(client.cookies[COOKIE])


def test_login_redirect_and_real_user_table(config: Config) -> None:
    app = create_app(config)
    with TestClient(app) as client:
        assert client.get("/", follow_redirects=False).headers["location"] == "/login"
        assert client.post("/login", data={"password": "wrong"}).status_code == 401

        response = client.post(
            "/api/actors",
            headers=_headers(),
            json={
                "actor": _actor(2002, username="philippe", first_name="Philippe", last_name="Test"),
                "blocked": True,
            },
        )
        assert response.status_code == 200
        _login(client)
        page = client.get("/")
        assert page.status_code == 200
        assert "Philippe Test" in page.text
        assert "@philippe" in page.text
        assert "2002" in page.text
        assert '"users"' not in page.text
        assert "Active user" not in page.text
        assert "#fffbf5" in client.get("/assets/app.css").text


def test_dashboard_paginates_requests_and_activity_independently(config: Config) -> None:
    app = create_app(config)
    with TestClient(app) as client:
        runtime = app.state.runtime
        actor = Actor(user_id=1001, chat_id=1001)
        for number in range(30):
            runtime.store.record_request(
                media_type="movie",
                external_id=10_000 + number,
                seasons=(),
                title=f"Movie {number}",
                year=2026,
                actor=actor,
            )
            runtime.store.record_activity("policy", f"Policy {number}")
        _login(client)

        page = client.get("/?request_page=2&activity_page=3")

        assert page.status_code == 200
        assert "Page 2 of 2 · 30 total" in page.text
        assert "Page 3 of 3 · 60 total" in page.text
        assert "request_page=1&amp;activity_page=3#requests" in page.text
        assert "request_page=2&amp;activity_page=2#activity" in page.text
        assert client.get("/?request_page=" + "9" * 10_000).status_code == 200


def test_dashboard_plex_activity_includes_episode_hierarchy(config: Config) -> None:
    app = create_app(config)
    with TestClient(app) as client:
        app.state.runtime.store.add_media_event(
            event_key="episode:42",
            media_type="series",
            external_id=411959,
            rating_key="42",
            title="Countdown",
            show_title="3 Body Problem",
            season_number=1,
            episode_number=2,
            plex_url="https://app.plex.tv/42",
        )
        _login(client)

        page = client.get("/")

        assert "Plex added 3 Body Problem · S01E02 · Countdown" in page.text


def test_dashboard_adds_and_removes_allowlisted_user(config: Config) -> None:
    app = create_app(config)
    with TestClient(app) as client:
        token = _login(client)
        csrf = app.state.runtime.sessions.csrf(token)
        response = client.post(
            "/users/add", data={"csrf": csrf, "user_id": "2002"}, follow_redirects=False
        )
        assert response.status_code == 303
        assert app.state.runtime.policy.snapshot().role(2002).value == "user"
        response = client.post(
            "/users/remove",
            data={"csrf": csrf, "user_id": "2002"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert app.state.runtime.policy.snapshot().role(2002).value == "blocked"


def test_roles_and_no_selection_token(config: Config) -> None:
    app = create_app(config)
    fake = FakeUpstream()
    fake.responses["radarr_search_movie"] = {
        "data": [
            {
                "tmdbId": 123,
                "title": "A Movie",
                "year": 2026,
                "overview": "Summary",
                "remotePoster": "https://image.tmdb.org/t/p/original/poster.jpg",
            }
        ]
    }
    fake.responses["sonarr_search_series"] = {"data": []}
    fake.tool_schemas = [
        {
            "name": "radarr_get_movies",
            "description": "Get movies",
            "inputSchema": {"type": "object"},
        },
        {"name": "unexpected_tool", "description": "No", "inputSchema": {}},
    ]
    with TestClient(app) as client:
        app.state.runtime.upstream = fake
        app.state.runtime.tools.upstream = fake
        user_tools = client.post("/api/tools", headers=_headers(), json=_actor(1001)).json()
        assert user_tools["role"] == "user"
        assert len(user_tools["tools"]) == len(SHARED_TOOLS)
        assert "radarr_get_movies" not in {tool["name"] for tool in user_tools["tools"]}
        admin_tools = client.post("/api/tools", headers=_headers(), json=_actor(9001)).json()
        assert "radarr_get_movies" in {tool["name"] for tool in admin_tools["tools"]}
        assert "unexpected_tool" not in {tool["name"] for tool in admin_tools["tools"]}

        response = client.post(
            "/api/tools/call",
            headers=_headers(),
            json={
                "actor": _actor(1001),
                "name": "search_media",
                "arguments": {"query": "A Movie", "media_type": "movie"},
            },
        )
        assert response.status_code == 200
        candidate = response.json()["result"]["results"][0]
        assert candidate["tmdb_id"] == 123
        assert candidate["poster_url"] == "https://image.tmdb.org/t/p/original/poster.jpg"
        assert "selection_token" not in json.dumps(response.json())

        denied = client.post(
            "/api/tools/call",
            headers=_headers(),
            json={"actor": _actor(1001), "name": "radarr_get_movies", "arguments": {}},
        )
        assert denied.status_code == 400


class _SlugClient:
    """Stand-in for Plex's metadata-match endpoint, counting every request."""

    requests: ClassVar[list[dict[str, Any]]] = []
    slug: ClassVar[str | None] = "dune-part-two"

    async def __aenter__(self) -> _SlugClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def get(self, url: str, **values: Any) -> Any:
        type(self).requests.append({"url": url, **values})
        payload = (
            {"MediaContainer": {"Metadata": [{"slug": type(self).slug}]}}
            if type(self).slug is not None
            else {"MediaContainer": {}}
        )
        return SimpleNamespace(is_error=False, json=lambda: payload)


@pytest.fixture
def slug_client(config: Config, monkeypatch: pytest.MonkeyPatch) -> type[_SlugClient]:
    _SlugClient.requests = []
    _SlugClient.slug = "dune-part-two"
    # The slug lookup reads the Plex token from the upstream credentials file.
    config.upstream_token_file.write_text(
        f"MCP_AUTH_TOKEN={'u' * 40}\nPLEX_API_KEY=plex-secret\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        "media_gateway.plex_watch.httpx.AsyncClient", lambda **_kwargs: _SlugClient()
    )
    return _SlugClient


def test_lone_downloaded_movie_links_with_the_form_that_opens_the_app(
    config: Config, slug_client: type[_SlugClient]
) -> None:
    """watch.plex.tv is the only form the Plex apps register as a universal link.

    A link naming this server's item (app.plex.tv/desktop/#!/server/...) opens
    the browser client instead, which is worse even though it skips a tap.
    """

    app = create_app(config)
    fake = FakeUpstream()
    fake.responses["radarr_search_movie"] = {
        "data": [{"tmdbId": 693134, "title": "Dune: Part Two", "year": 2024, "hasFile": True}]
    }
    with TestClient(app) as client:
        app.state.runtime.tools.upstream = fake
        response = client.post(
            "/api/tools/call",
            headers=_headers(),
            json={
                "actor": _actor(1001),
                "name": "search_media",
                "arguments": {"query": "Dune Part Two", "media_type": "movie", "limit": 1},
            },
        )

    assert response.status_code == 200
    result = response.json()["result"]["results"][0]
    assert result["plex_url"] == "https://watch.plex.tv/movie/dune-part-two"
    # One GUID match, and no library traversal at all.
    assert len(slug_client.requests) == 1
    assert slug_client.requests[0]["params"] == {"guid": "tmdb://693134", "type": 1}
    assert [name for name, _ in fake.calls if name.startswith("plex_")] == []


def test_movie_without_a_plex_slug_gets_no_link(
    config: Config, slug_client: type[_SlugClient]
) -> None:
    """Plex knows no slug, so there is no app-opening link to offer."""

    slug_client.slug = None
    app = create_app(config)
    fake = FakeUpstream()
    fake.responses["radarr_search_movie"] = {
        "data": [{"tmdbId": 693134, "title": "Dune: Part Two", "year": 2024, "hasFile": True}]
    }
    with TestClient(app) as client:
        app.state.runtime.tools.upstream = fake
        response = client.post(
            "/api/tools/call",
            headers=_headers(),
            json={
                "actor": _actor(1001),
                "name": "search_media",
                "arguments": {"query": "Dune Part Two", "media_type": "movie", "limit": 1},
            },
        )

    assert "plex_url" not in response.json()["result"]["results"][0]


def test_a_held_series_gets_an_open_in_plex_link(
    config: Config, slug_client: type[_SlugClient]
) -> None:
    """A lone series card links to Plex once any season is complete."""

    slug_client.slug = "severance"
    app = create_app(config)
    fake = FakeUpstream()
    fake.responses["sonarr_search_series"] = {
        "data": [
            {
                "tvdbId": 371980,
                "title": "Severance",
                "year": 2022,
                "id": 4,
                "seasons": [{"seasonNumber": 1}, {"seasonNumber": 2}],
            }
        ]
    }
    fake.responses["sonarr_get_series_by_id"] = {
        "data": {
            "id": 4,
            "seasons": [
                {"seasonNumber": 1, "statistics": {"episodeFileCount": 9, "totalEpisodeCount": 9}},
                {"seasonNumber": 2, "statistics": {"episodeFileCount": 0, "totalEpisodeCount": 10}},
            ],
        }
    }
    with TestClient(app) as client:
        app.state.runtime.tools.upstream = fake
        response = client.post(
            "/api/tools/call",
            headers=_headers(),
            json={
                "actor": _actor(1001),
                "name": "search_media",
                "arguments": {"query": "Severance", "media_type": "series", "limit": 1},
            },
        )

    result = response.json()["result"]["results"][0]
    # Partly held is still watchable, so the link is offered.
    assert result["seasons_complete"] == [1]
    assert result["plex_url"] == "https://watch.plex.tv/show/severance"
    assert slug_client.requests[0]["params"] == {"guid": "tvdb://371980", "type": 2}


def test_a_series_with_nothing_held_gets_no_link(
    config: Config, slug_client: type[_SlugClient]
) -> None:
    app = create_app(config)
    fake = FakeUpstream()
    fake.responses["sonarr_search_series"] = {
        "data": [
            {
                "tvdbId": 371980,
                "title": "Severance",
                "year": 2022,
                "id": 4,
                "seasons": [{"seasonNumber": 1}],
            }
        ]
    }
    fake.responses["sonarr_get_series_by_id"] = {
        "data": {
            "id": 4,
            "seasons": [
                {"seasonNumber": 1, "statistics": {"episodeFileCount": 0, "totalEpisodeCount": 9}},
            ],
        }
    }
    with TestClient(app) as client:
        app.state.runtime.tools.upstream = fake
        response = client.post(
            "/api/tools/call",
            headers=_headers(),
            json={
                "actor": _actor(1001),
                "name": "search_media",
                "arguments": {"query": "Severance", "media_type": "series", "limit": 1},
            },
        )

    assert "plex_url" not in response.json()["result"]["results"][0]
    assert slug_client.requests == []


def test_multi_result_search_resolves_no_slugs(
    config: Config, slug_client: type[_SlugClient]
) -> None:
    """Only the single-result card renders a link, so a picker costs no lookups."""

    app = create_app(config)
    fake = FakeUpstream()
    fake.responses["radarr_search_movie"] = {
        "data": [
            {"tmdbId": 1, "title": "Dune", "year": 1984, "hasFile": True},
            {"tmdbId": 2, "title": "Dune", "year": 2021, "hasFile": True},
            {"tmdbId": 3, "title": "Dune: Part Two", "year": 2024, "hasFile": True},
        ]
    }
    with TestClient(app) as client:
        app.state.runtime.tools.upstream = fake
        response = client.post(
            "/api/tools/call",
            headers=_headers(),
            json={
                "actor": _actor(1001),
                "name": "search_media",
                "arguments": {"query": "Dune", "media_type": "movie", "limit": 3},
            },
        )

    assert len(response.json()["result"]["results"]) == 3
    assert slug_client.requests == []


def test_recommendations_resolve_no_slugs(config: Config, slug_client: type[_SlugClient]) -> None:
    """Recommendations answer conversationally and render no buttons."""

    app = create_app(config)
    fake = FakeUpstream()

    def movies(arguments: dict[str, Any]) -> dict[str, Any]:
        term = str(arguments.get("term", ""))
        title, year = term.rsplit(" ", 1)
        return {
            "data": [
                {
                    "tmdbId": abs(hash(term)) % 9999,
                    "title": title,
                    "year": int(year.strip("()")),
                    "hasFile": True,
                }
            ]
        }

    fake.responses["radarr_search_movie"] = movies
    with TestClient(app) as client:
        app.state.runtime.tools.upstream = fake
        response = client.post(
            "/api/tools/call",
            headers=_headers(),
            json={
                "actor": _actor(1001),
                "name": "recommend_media",
                "arguments": {
                    "titles": [
                        "Arrival (2016)",
                        "Ex Machina (2014)",
                        "Annihilation (2018)",
                        "Dark City (1998)",
                    ],
                    "media_type": "movie",
                },
            },
        )

    assert response.status_code == 200
    assert slug_client.requests == []


def test_series_search_resolves_no_slugs(config: Config, slug_client: type[_SlugClient]) -> None:
    """A series card opens the season picker, which reports Sonarr's own state."""

    app = create_app(config)
    fake = FakeUpstream()
    fake.responses["sonarr_search_series"] = {
        "data": [{"tvdbId": 411959, "title": "3 Body Problem", "year": 2024, "id": 7}]
    }
    with TestClient(app) as client:
        app.state.runtime.tools.upstream = fake
        response = client.post(
            "/api/tools/call",
            headers=_headers(),
            json={
                "actor": _actor(1001),
                "name": "search_media",
                "arguments": {"query": "3 Body Problem", "media_type": "series", "limit": 1},
            },
        )

    result = response.json()["result"]["results"][0]
    assert "plex_url" not in result
    assert slug_client.requests == []
    assert [name for name, _ in fake.calls if name.startswith("plex_")] == []


def test_undownloaded_movie_resolves_no_slug(
    config: Config, slug_client: type[_SlugClient]
) -> None:
    app = create_app(config)
    fake = FakeUpstream()
    fake.responses["radarr_search_movie"] = {
        "data": [{"tmdbId": 693134, "title": "Dune: Part Two", "year": 2024, "hasFile": False}]
    }
    with TestClient(app) as client:
        app.state.runtime.tools.upstream = fake
        response = client.post(
            "/api/tools/call",
            headers=_headers(),
            json={
                "actor": _actor(1001),
                "name": "search_media",
                "arguments": {"query": "Dune Part Two", "media_type": "movie", "limit": 1},
            },
        )

    assert "plex_url" not in response.json()["result"]["results"][0]
    assert slug_client.requests == []


def test_mixed_search_keeps_movie_and_series_results(config: Config) -> None:
    app = create_app(config)
    fake = FakeUpstream()
    fake.responses["radarr_search_movie"] = {
        "data": [
            {"tmdbId": 1, "title": "Movie One", "year": 2024},
            {"tmdbId": 2, "title": "Movie Two", "year": 2025},
        ]
    }
    fake.responses["sonarr_search_series"] = {
        "data": [{"tvdbId": 3, "title": "Series One", "year": 2026}]
    }
    with TestClient(app) as client:
        app.state.runtime.tools.upstream = fake
        response = client.post(
            "/api/tools/call",
            headers=_headers(),
            json={
                "actor": _actor(1001),
                "name": "search_media",
                "arguments": {"query": "One", "limit": 2},
            },
        )
        assert response.status_code == 200
        assert [item["media_type"] for item in response.json()["result"]["results"]] == [
            "movie",
            "series",
        ]


def test_recommendations_return_one_distinct_exact_match_per_title(config: Config) -> None:
    app = create_app(config)
    fake = FakeUpstream()

    def movies(arguments: dict[str, Any]) -> dict[str, Any]:
        rows = {
            "Arrival (2016)": [
                {"tmdbId": 10, "title": "Arrival", "year": 2025},
                {"tmdbId": 11, "title": "Arrival", "year": 2016},
            ],
            "Ex Machina (2014)": [
                {"tmdbId": 12, "title": "Ex Machina", "year": 2014},
                {"tmdbId": 120, "title": "Ex Machina: Extra", "year": 2015},
            ],
            "Annihilation (2018)": [
                {"tmdbId": 13, "title": "Annihilation", "year": 2018},
            ],
            "Dark City (1998)": [
                {"tmdbId": 14, "title": "Dark City", "year": 1998},
            ],
        }
        return {"data": rows[arguments["term"]]}

    fake.responses["radarr_search_movie"] = movies
    with TestClient(app) as client:
        app.state.runtime.tools.upstream = fake
        response = client.post(
            "/api/tools/call",
            headers=_headers(),
            json={
                "actor": _actor(1001),
                "name": "recommend_media",
                "arguments": {
                    "titles": [
                        "Arrival (2016)",
                        "Ex Machina (2014)",
                        "Annihilation (2018)",
                        "Dark City (1998)",
                    ],
                    "media_type": "movie",
                },
            },
        )

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["presentation"] == "recommendations"
    assert [item["tmdb_id"] for item in result["results"]] == [11, 12, 13, 14]
    assert [call[1]["term"] for call in fake.calls] == [
        "Arrival (2016)",
        "Ex Machina (2014)",
        "Annihilation (2018)",
        "Dark City (1998)",
    ]


def test_recommendations_reject_the_same_title_with_different_years(config: Config) -> None:
    app = create_app(config)
    with TestClient(app) as client:
        response = client.post(
            "/api/tools/call",
            headers=_headers(),
            json={
                "actor": _actor(1001),
                "name": "recommend_media",
                "arguments": {
                    "titles": [
                        "Arrival (2016)",
                        "Arrival (2025)",
                        "Ex Machina (2014)",
                        "Dark City (1998)",
                    ],
                    "media_type": "movie",
                },
            },
        )

    assert response.status_code == 400
    assert response.json()["error"] == "titles must be distinct"


def test_recommendations_omit_inexact_provider_matches(config: Config) -> None:
    app = create_app(config)
    fake = FakeUpstream()
    fake.responses["radarr_search_movie"] = {
        "data": [{"tmdbId": 10, "title": "Arrival: The Journey", "year": 2016}]
    }
    with TestClient(app) as client:
        app.state.runtime.tools.upstream = fake
        response = client.post(
            "/api/tools/call",
            headers=_headers(),
            json={
                "actor": _actor(1001),
                "name": "recommend_media",
                "arguments": {
                    "titles": [
                        "Arrival (2016)",
                        "Ex Machina (2014)",
                        "Annihilation (2018)",
                        "Dark City (1998)",
                    ],
                    "media_type": "movie",
                },
            },
        )

    assert response.status_code == 200
    assert response.json()["result"]["results"] == []


def test_existing_missing_movie_starts_a_search(config: Config) -> None:
    app = create_app(config)
    fake = FakeUpstream()
    fake.responses["radarr_search_movie"] = {
        "data": [
            {
                "id": 77,
                "tmdbId": 123,
                "title": "A Movie",
                "year": 2026,
                "hasFile": False,
            }
        ]
    }
    with TestClient(app) as client:
        app.state.runtime.tools.upstream = fake
        response = client.post(
            "/api/tools/call",
            headers=_headers(),
            json={
                "actor": _actor(1001),
                "name": "request_movie",
                "arguments": {"tmdb_id": 123},
            },
        )
        assert response.status_code == 200
        assert response.json()["result"]["status"] == "search_started"
        assert ("radarr_search_movie_releases", {"id": 77}) in fake.calls


def test_movie_is_available_only_after_exact_plex_match(config: Config) -> None:
    app = create_app(config)
    fake = FakeUpstream()
    fake.responses["radarr_search_movie"] = {
        "data": [{"id": 77, "tmdbId": 123, "title": "A Movie", "year": 2026, "hasFile": True}]
    }
    fake.responses["plex_search"] = {
        "MediaContainer": {"Hub": [{"Metadata": [{"type": "movie", "ratingKey": "42"}]}]}
    }
    fake.responses["plex_get_metadata"] = {
        "MediaContainer": {"Metadata": [{"Guid": [{"id": "tmdb://123"}]}]}
    }
    with TestClient(app) as client:
        app.state.runtime.tools.upstream = fake
        response = client.post(
            "/api/tools/call",
            headers=_headers(),
            json={
                "actor": _actor(1001),
                "name": "request_movie",
                "arguments": {"tmdb_id": 123},
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["result"]["status"] == "available"
        assert app.state.runtime.store.requests_for(1001)[0]["state"] == "available"


def test_downloaded_movie_not_in_plex_remains_pending(config: Config) -> None:
    app = create_app(config)
    fake = FakeUpstream()
    fake.responses["radarr_search_movie"] = {
        "data": [{"id": 77, "tmdbId": 123, "title": "A Movie", "year": 2026, "hasFile": True}]
    }
    fake.responses["plex_search"] = {"MediaContainer": {"Hub": []}}
    with TestClient(app) as client:
        app.state.runtime.tools.upstream = fake
        response = client.post(
            "/api/tools/call",
            headers=_headers(),
            json={
                "actor": _actor(1001),
                "name": "request_movie",
                "arguments": {"tmdb_id": 123},
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["result"]["status"] == "awaiting_plex"
        assert app.state.runtime.store.requests_for(1001)[0]["state"] == "requested"


async def test_movie_intent_is_durable_before_mutation_and_reconciles(config: Config) -> None:
    app = create_app(config)
    fake = FakeUpstream()
    fake.responses["radarr_search_movie"] = {
        "data": [{"tmdbId": 123, "title": "A Movie", "year": 2026, "hasFile": False}]
    }
    with TestClient(app) as client:
        runtime = app.state.runtime
        runtime.tools.upstream = fake

        def fail_add(_arguments: dict[str, Any]) -> object:
            intent = runtime.store.requests_for(1001)[0]
            assert intent["state"] == "pending"
            assert intent["destinations"] == [1001]
            raise UpstreamError("interrupted after provider call")

        fake.responses["radarr_add_movie"] = fail_add
        response = client.post(
            "/api/tools/call",
            headers=_headers(),
            json={
                "actor": _actor(1001),
                "name": "request_movie",
                "arguments": {"tmdb_id": 123},
            },
        )
        assert response.status_code == 502
        assert runtime.store.requests_for(1001)[0]["state"] == "unknown"

        fake.responses["radarr_search_movie"] = {
            "data": [{"id": 77, "tmdbId": 123, "title": "A Movie", "year": 2026, "hasFile": False}]
        }
        report = await runtime.tools.reconcile_pending_requests()

        assert report == {"repaired": 1, "unresolved": 0}
        repaired = runtime.store.requests_for(1001)[0]
        assert repaired["state"] == "requested"
        assert repaired["provider_status"] == "search_started"
        assert ("radarr_search_movie_releases", {"id": 77}) in fake.calls


def test_series_intent_is_durable_when_provider_mutation_fails(config: Config) -> None:
    app = create_app(config)
    fake = FakeUpstream()
    fake.responses["sonarr_search_series"] = {
        "data": [
            {
                "tvdbId": 411959,
                "title": "3 Body Problem",
                "year": 2024,
                "seasons": [{"seasonNumber": 1}],
            }
        ]
    }

    def fail_add(_arguments: dict[str, Any]) -> object:
        raise UpstreamError("interrupted Sonarr operation")

    fake.responses["sonarr_add_series"] = fail_add
    with TestClient(app) as client:
        app.state.runtime.tools.upstream = fake
        response = client.post(
            "/api/tools/call",
            headers=_headers(),
            json={
                "actor": _actor(1001),
                "name": "request_series",
                "arguments": {"tvdb_id": 411959, "seasons": [1], "anime": False},
            },
        )

        assert response.status_code == 502
        intent = app.state.runtime.store.requests_for(1001)[0]
        assert intent["state"] == "unknown"
        assert intent["options"] == {"anime": False}
        assert intent["destinations"] == [1001]


def test_download_status_includes_both_queues_for_regular_users(config: Config) -> None:
    app = create_app(config)
    fake = FakeUpstream()
    fake.responses["radarr_queue"] = [
        {"title": "Movie download", "status": "downloading", "sizeleft": 10}
    ]
    fake.responses["sonarr_get_queue"] = {
        "data": [{"seriesTitle": "Series download", "status": "downloading"}]
    }
    fake.responses["radarr_get_movies"] = {"data": []}
    with TestClient(app) as client:
        app.state.runtime.tools.upstream = fake
        response = client.post(
            "/api/tools/call",
            headers=_headers(),
            json={
                "actor": _actor(1001),
                "name": "download_status",
                "arguments": {},
            },
        )
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert result["movie_downloads"][0]["title"] == "Movie download"
        assert result["series_downloads"][0]["title"] == "Series download"
        assert result["unavailable_sources"] == []


def test_upstream_errors_do_not_leak_provider_details(config: Config) -> None:
    app = create_app(config)
    fake = FakeUpstream()

    def fail(_arguments: dict[str, Any]) -> object:
        raise UpstreamError("http://radarr:7878/api?apikey=secret")

    fake.responses["radarr_search_movie"] = fail
    with TestClient(app) as client:
        app.state.runtime.tools.upstream = fake
        response = client.post(
            "/api/tools/call",
            headers=_headers(),
            json={
                "actor": _actor(1001),
                "name": "search_media",
                "arguments": {"query": "Movie", "media_type": "movie"},
            },
        )
        assert response.status_code == 502
        assert response.json()["error"] == "media service is temporarily unavailable"
        assert "secret" not in response.text


def test_schema_endpoint_requires_every_pinned_admin_tool(config: Config) -> None:
    app = create_app(config)
    fake = FakeUpstream()
    fake.tool_schemas = [
        {"name": name, "description": name, "inputSchema": {"type": "object"}}
        for name in sorted(ADMIN_UPSTREAM_TOOLS)
    ] + [{"name": "future_tool", "description": "ignored", "inputSchema": {}}]
    with TestClient(app) as client:
        app.state.runtime.tools.upstream = fake
        unauthorized = client.get("/api/schema")
        assert unauthorized.status_code == 401
        response = client.get("/api/schema", headers=_headers())
        assert response.status_code == 200
        tools = response.json()["tools"]
        expected = {name: "shared" for name in SHARED_TOOLS}
        expected.update({name: "admin" for name in ADMIN_UPSTREAM_TOOLS})
        assert {item["name"]: item["scope"] for item in tools} == expected
        assert len(tools) == len(expected)
        assert "future_tool" not in {item["name"] for item in tools}


def test_admin_proxy_preserves_nested_upstream_results(config: Config) -> None:
    app = create_app(config)
    fake = FakeUpstream()
    fake.responses["radarr_get_movies"] = {
        "data": [{"title": "Nested", "quality": {"profile": {"name": "HD"}}}],
        "total": 1,
    }
    with TestClient(app) as client:
        app.state.runtime.tools.upstream = fake
        response = client.post(
            "/api/tools/call",
            headers=_headers(),
            json={
                "actor": _actor(9001),
                "name": "radarr_get_movies",
                "arguments": {"limit": 1},
            },
        )
        assert response.status_code == 200
        assert response.json()["result"]["result"]["data"][0]["quality"] == {
            "profile": {"name": "HD"}
        }


def test_series_request_uses_trusted_actor_and_exact_seasons(config: Config) -> None:
    app = create_app(config)
    fake = FakeUpstream()
    fake.responses["sonarr_search_series"] = {
        "data": [
            {
                "tvdbId": 411959,
                "title": "3 Body Problem",
                "year": 2024,
                "seasons": [{"seasonNumber": 1}],
            }
        ]
    }
    fake.responses["sonarr_add_series"] = {"id": 7}
    with TestClient(app) as client:
        app.state.runtime.upstream = fake
        app.state.runtime.tools.upstream = fake
        response = client.post(
            "/api/tools/call",
            headers=_headers(),
            json={
                "actor": _actor(1001, username="trusted-user"),
                "name": "request_series",
                "arguments": {"tvdb_id": 411959, "seasons": [1]},
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["result"]["series"]["title"] == "3 Body Problem"
        add = next(arguments for name, arguments in fake.calls if name == "sonarr_add_series")
        assert add["seasons"] == [{"seasonNumber": 1, "monitored": True}]
        requests = app.state.runtime.store.requests_for(1001)
        assert requests[0]["user_id"] == 1001
        assert requests[0]["seasons"] == [1]


def test_specials_notify_only_the_specials_requester(config: Config) -> None:
    """A season-0 event must not reach everyone who asked for other seasons.

    Season 0 used to be parsed as "no season", and a missing season number
    matches every outstanding requester of the show.
    """

    app = create_app(config)
    with TestClient(app) as client:
        runtime = app.state.runtime
        runtime.store.record_request(
            media_type="series",
            external_id=371980,
            seasons=(0,),
            title="Severance",
            year=2022,
            actor=Actor(user_id=1001, chat_id=1001),
        )
        runtime.store.record_request(
            media_type="series",
            external_id=371980,
            seasons=(1,),
            title="Severance",
            year=2022,
            actor=Actor(user_id=2002, chat_id=2002),
        )

        assert runtime.store.requested_seasons(371980) == {0, 1}

        specials = runtime.store.request_destinations(
            media_type="series", external_id=371980, season_number=0
        )
        season_one = runtime.store.request_destinations(
            media_type="series", external_id=371980, season_number=1
        )

        assert specials == {(1001, 1001)}
        assert season_one == {(2002, 2002)}
        assert client.get("/login").status_code == 200


def test_specials_webhook_keeps_its_season_number(config: Config) -> None:
    app = create_app(config)
    payload = {
        "event": "library.new",
        "Metadata": {
            "type": "episode",
            "ratingKey": "3001",
            "title": "The Lexington Letter",
            "index": 1,
            "parentIndex": 0,
            "grandparentTitle": "Severance",
            "grandparentGuid": "tvdb://371980",
            "grandparentRatingKey": "1757",
        },
    }
    token = "plex-hook-secret-with-at-least-32-bytes"
    with TestClient(app) as client:
        assert client.post(f"/private/plex/{token}", json=payload).json() == {"accepted": True}
        event = app.state.runtime.store.pending_media_events(int(time.time()) + 10)[0]

    # 0 is the specials season, not a missing season.
    assert event["season_number"] == 0
    assert event["episode_number"] == 1


def test_availability_comes_from_the_radarr_library_not_the_lookup(config: Config) -> None:
    """Radarr's lookup reports hasFile as null even for a film on disk.

    Reading availability there marks every title as missing, so the bot offers
    to add films the user can already watch.
    """

    app = create_app(config)
    fake = FakeUpstream()
    fake.responses["radarr_search_movie"] = {
        "data": [
            # Tracked (has an id) but the lookup omits hasFile.
            {"tmdbId": 299536, "title": "Avengers: Infinity War", "year": 2018, "id": 198},
            # Not tracked at all: no library record can exist.
            {"tmdbId": 999999, "title": "Some Unowned Film", "year": 2024},
        ]
    }
    fake.responses["radarr_get_movie"] = {
        "data": {"id": 198, "title": "Avengers: Infinity War", "hasFile": True}
    }
    with TestClient(app) as client:
        app.state.runtime.tools.upstream = fake
        response = client.post(
            "/api/tools/call",
            headers=_headers(),
            json={
                "actor": _actor(1001),
                "name": "search_media",
                "arguments": {"query": "Avengers", "media_type": "movie", "limit": 2},
            },
        )

    results = {item["title"]: item for item in response.json()["result"]["results"]}
    assert results["Avengers: Infinity War"]["downloaded"] is True
    assert results["Some Unowned Film"]["downloaded"] is False
    # One library read, and only for the tracked title.
    assert [args for name, args in fake.calls if name == "radarr_get_movie"] == [{"id": 198}]


def test_recommendations_report_owned_titles_as_available(config: Config) -> None:
    """The reported case: four owned films all shown as missing."""

    app = create_app(config)
    fake = FakeUpstream()
    owned = {
        "Avengers: Infinity War (2018)": (299536, 198, 2018),
        "Avengers: Endgame (2019)": (299534, 197, 2019),
        "Doctor Strange in the Multiverse of Madness (2022)": (453395, 760, 2022),
        "Spider-Man: No Way Home (2021)": (634649, 500, 2021),
    }

    def lookup(arguments: dict[str, Any]) -> dict[str, Any]:
        term = str(arguments.get("term", ""))
        tmdb, radarr_id, year = owned[term]
        return {
            "data": [
                {
                    "tmdbId": tmdb,
                    "title": term.rsplit(" (", 1)[0],
                    "year": year,
                    "id": radarr_id,
                }
            ]
        }

    def library(arguments: dict[str, Any]) -> dict[str, Any]:
        return {"data": {"id": arguments["id"], "hasFile": True}}

    fake.responses["radarr_search_movie"] = lookup
    fake.responses["radarr_get_movie"] = library
    with TestClient(app) as client:
        app.state.runtime.tools.upstream = fake
        response = client.post(
            "/api/tools/call",
            headers=_headers(),
            json={
                "actor": _actor(1001),
                "name": "recommend_media",
                "arguments": {"titles": list(owned), "media_type": "movie"},
            },
        )

    results = response.json()["result"]["results"]
    assert len(results) == 4
    assert all(item["downloaded"] is True for item in results)


def test_untracked_movies_cost_no_library_reads(config: Config) -> None:
    app = create_app(config)
    fake = FakeUpstream()
    fake.responses["radarr_search_movie"] = {
        "data": [
            {"tmdbId": 1, "title": "Unowned One", "year": 2024},
            {"tmdbId": 2, "title": "Unowned Two", "year": 2025},
        ]
    }
    with TestClient(app) as client:
        app.state.runtime.tools.upstream = fake
        response = client.post(
            "/api/tools/call",
            headers=_headers(),
            json={
                "actor": _actor(1001),
                "name": "search_media",
                "arguments": {"query": "Unowned", "media_type": "movie", "limit": 2},
            },
        )

    assert all(item["downloaded"] is False for item in response.json()["result"]["results"])
    assert [name for name, _ in fake.calls if name == "radarr_get_movie"] == []


def test_recommendations_name_the_titles_they_could_not_find(config: Config) -> None:
    """Four researched titles must not quietly become three suggestions."""

    app = create_app(config)
    fake = FakeUpstream()

    def lookup(arguments: dict[str, Any]) -> dict[str, Any]:
        term = str(arguments.get("term", ""))
        if term.startswith("Some Film That Does Not Exist"):
            return {"data": []}
        title, year = term.rsplit(" (", 1)
        return {
            "data": [
                {"tmdbId": abs(hash(term)) % 9999, "title": title, "year": int(year.rstrip(")"))}
            ]
        }

    fake.responses["radarr_search_movie"] = lookup
    with TestClient(app) as client:
        app.state.runtime.tools.upstream = fake
        response = client.post(
            "/api/tools/call",
            headers=_headers(),
            json={
                "actor": _actor(1001),
                "name": "recommend_media",
                "arguments": {
                    "titles": [
                        "Arrival (2016)",
                        "Ex Machina (2014)",
                        "Some Film That Does Not Exist (2019)",
                        "Dark City (1998)",
                    ],
                    "media_type": "movie",
                },
            },
        )

    result = response.json()["result"]
    assert len(result["results"]) == 3
    assert result["unmatched_titles"] == ["Some Film That Does Not Exist (2019)"]


def test_series_results_report_which_seasons_are_held(config: Config) -> None:
    """A search result alone cannot tell a held series from a tracked one."""

    app = create_app(config)
    fake = FakeUpstream()
    fake.responses["sonarr_search_series"] = {
        "data": [
            {
                "tvdbId": 371980,
                "title": "Severance",
                "year": 2022,
                "id": 4,
                "seasons": [{"seasonNumber": 1}, {"seasonNumber": 2}],
            }
        ]
    }
    fake.responses["sonarr_get_series_by_id"] = {
        "data": {
            "id": 4,
            "seasons": [
                {"seasonNumber": 1, "statistics": {"episodeFileCount": 9, "totalEpisodeCount": 9}},
                {"seasonNumber": 2, "statistics": {"episodeFileCount": 0, "totalEpisodeCount": 10}},
            ],
        }
    }
    with TestClient(app) as client:
        app.state.runtime.tools.upstream = fake
        response = client.post(
            "/api/tools/call",
            headers=_headers(),
            json={
                "actor": _actor(1001),
                "name": "search_media",
                "arguments": {"query": "Severance", "media_type": "series", "limit": 1},
            },
        )

    result = response.json()["result"]["results"][0]
    assert result["seasons_complete"] == [1]
    assert result["seasons_missing"] == [2]
    # Half a show is not "available".
    assert result["downloaded"] is False


def test_fully_held_series_is_reported_as_downloaded(config: Config) -> None:
    app = create_app(config)
    fake = FakeUpstream()
    fake.responses["sonarr_search_series"] = {
        "data": [
            {
                "tvdbId": 371980,
                "title": "Severance",
                "year": 2022,
                "id": 4,
                "seasons": [{"seasonNumber": 1}],
            }
        ]
    }
    fake.responses["sonarr_get_series_by_id"] = {
        "data": {
            "id": 4,
            "seasons": [
                {"seasonNumber": 1, "statistics": {"episodeFileCount": 9, "totalEpisodeCount": 9}}
            ],
        }
    }
    with TestClient(app) as client:
        app.state.runtime.tools.upstream = fake
        response = client.post(
            "/api/tools/call",
            headers=_headers(),
            json={
                "actor": _actor(1001),
                "name": "search_media",
                "arguments": {"query": "Severance", "media_type": "series", "limit": 1},
            },
        )

    result = response.json()["result"]["results"][0]
    assert result["downloaded"] is True
    assert result["seasons_missing"] == []


def test_untracked_series_costs_no_library_read(config: Config) -> None:
    app = create_app(config)
    fake = FakeUpstream()
    fake.responses["sonarr_search_series"] = {
        "data": [
            {"tvdbId": 371980, "title": "Severance", "year": 2022, "seasons": [{"seasonNumber": 1}]}
        ]
    }
    with TestClient(app) as client:
        app.state.runtime.tools.upstream = fake
        response = client.post(
            "/api/tools/call",
            headers=_headers(),
            json={
                "actor": _actor(1001),
                "name": "search_media",
                "arguments": {"query": "Severance", "media_type": "series", "limit": 1},
            },
        )

    assert "seasons_complete" not in response.json()["result"]["results"][0]
    assert [name for name, _ in fake.calls if name == "sonarr_get_series_by_id"] == []


def test_an_unaired_season_is_not_reported_missing(config: Config) -> None:
    """A season Sonarr lists with no episodes has nothing to acquire.

    Counting it as missing would offer to add a season that does not exist yet
    and keep every ongoing show permanently unavailable.
    """

    app = create_app(config)
    fake = FakeUpstream()
    fake.responses["sonarr_search_series"] = {
        "data": [
            {
                "tvdbId": 371980,
                "title": "Ongoing Show",
                "year": 2022,
                "id": 4,
                "seasons": [{"seasonNumber": 1}, {"seasonNumber": 2}, {"seasonNumber": 3}],
            }
        ]
    }
    fake.responses["sonarr_get_series_by_id"] = {
        "data": {
            "id": 4,
            "seasons": [
                {
                    "seasonNumber": 1,
                    "statistics": {"episodeFileCount": 10, "totalEpisodeCount": 10},
                },
                {"seasonNumber": 2, "statistics": {"episodeFileCount": 8, "totalEpisodeCount": 8}},
                # Announced, nothing aired.
                {"seasonNumber": 3, "statistics": {"episodeFileCount": 0, "totalEpisodeCount": 0}},
            ],
        }
    }
    with TestClient(app) as client:
        app.state.runtime.tools.upstream = fake
        response = client.post(
            "/api/tools/call",
            headers=_headers(),
            json={
                "actor": _actor(1001),
                "name": "search_media",
                "arguments": {"query": "Ongoing", "media_type": "series", "limit": 1},
            },
        )

    result = response.json()["result"]["results"][0]
    assert result["seasons_complete"] == [1, 2]
    assert result["seasons_missing"] == []
    # Everything that exists is held, so the show reads as available.
    assert result["downloaded"] is True


def test_mixed_search_keeps_tmdb_and_tvdb_library_ids_separate(config: Config) -> None:
    """Equal external ids must not cross the Radarr/Sonarr namespace boundary."""

    app = create_app(config)
    fake = FakeUpstream()
    fake.responses["radarr_search_movie"] = {
        "data": [{"tmdbId": 42, "title": "Movie 42", "year": 2024, "id": 100}]
    }
    fake.responses["sonarr_search_series"] = {
        "data": [
            {
                "tvdbId": 42,
                "title": "Series 42",
                "year": 2024,
                "id": 200,
                "seasons": [{"seasonNumber": 1}],
            }
        ]
    }
    fake.responses["radarr_get_movie"] = {"data": {"id": 100, "title": "Movie 42", "hasFile": True}}
    fake.responses["sonarr_get_series_by_id"] = {
        "data": {
            "id": 200,
            "seasons": [
                {
                    "seasonNumber": 1,
                    "statistics": {"episodeFileCount": 8, "totalEpisodeCount": 8},
                }
            ],
        }
    }

    with TestClient(app) as client:
        app.state.runtime.tools.upstream = fake
        response = client.post(
            "/api/tools/call",
            headers=_headers(),
            json={
                "actor": _actor(1001),
                "name": "search_media",
                "arguments": {"query": "42", "media_type": "all", "limit": 2},
            },
        )

    results = {item["media_type"]: item for item in response.json()["result"]["results"]}
    assert results["movie"]["downloaded"] is True
    assert results["series"]["downloaded"] is True
    assert [args for name, args in fake.calls if name == "radarr_get_movie"] == [{"id": 100}]
    assert [args for name, args in fake.calls if name == "sonarr_get_series_by_id"] == [{"id": 200}]


def test_series_seasons_reports_counts_from_the_tracked_series(config: Config) -> None:
    """The lookup response leaves statistics null, so counts need the series itself."""

    app = create_app(config)
    fake = FakeUpstream()
    fake.responses["sonarr_search_series"] = {
        "data": [
            {
                "tvdbId": 371980,
                "title": "Severance",
                "year": 2022,
                "id": 4,
                "seasons": [
                    {"seasonNumber": 0, "monitored": False},
                    {"seasonNumber": 1, "monitored": True},
                    {"seasonNumber": 2, "monitored": False},
                ],
            }
        ]
    }
    fake.responses["sonarr_get_series_by_id"] = {
        "data": {
            "id": 4,
            "seasons": [
                {
                    "seasonNumber": 0,
                    "monitored": False,
                    "statistics": {"episodeFileCount": 0, "totalEpisodeCount": 21},
                },
                {
                    "seasonNumber": 1,
                    "monitored": True,
                    "statistics": {"episodeFileCount": 9, "totalEpisodeCount": 9},
                },
                {
                    "seasonNumber": 2,
                    "monitored": True,
                    "statistics": {"episodeFileCount": 0, "totalEpisodeCount": 10},
                },
                {
                    "seasonNumber": 3,
                    "monitored": True,
                    "statistics": {"episodeFileCount": 4, "totalEpisodeCount": 8},
                },
            ],
        }
    }
    with TestClient(app) as client:
        app.state.runtime.tools.upstream = fake
        response = client.post(
            "/api/tools/call",
            headers=_headers(),
            json={
                "actor": _actor(1001),
                "name": "series_seasons",
                "arguments": {"tvdb_id": 371980},
            },
        )

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["title"] == "Severance"
    assert result["in_sonarr"] is True
    by_number = {state["number"]: state for state in result["seasons"]}
    # Specials are reported rather than hidden.
    assert by_number[0]["episodes"] == 21
    assert by_number[0]["complete"] is False
    assert by_number[1]["complete"] is True
    # Monitored with no files means it was asked for and is still searching.
    assert by_number[2]["monitored"] is True
    assert by_number[2]["complete"] is False
    assert by_number[2]["partial"] is False
    assert by_number[3]["partial"] is True
    assert by_number[3]["complete"] is False


def test_series_seasons_handles_an_untracked_series(config: Config) -> None:
    app = create_app(config)
    fake = FakeUpstream()
    fake.responses["sonarr_search_series"] = {
        "data": [
            {
                "tvdbId": 371980,
                "title": "Severance",
                "year": 2022,
                "seasons": [{"seasonNumber": 1, "monitored": False}],
            }
        ]
    }
    with TestClient(app) as client:
        app.state.runtime.tools.upstream = fake
        response = client.post(
            "/api/tools/call",
            headers=_headers(),
            json={
                "actor": _actor(1001),
                "name": "series_seasons",
                "arguments": {"tvdb_id": 371980},
            },
        )

    result = response.json()["result"]
    assert result["in_sonarr"] is False
    assert result["seasons"] == [
        {
            "number": 1,
            "files": 0,
            "episodes": 0,
            "monitored": False,
            "complete": False,
            "partial": False,
        }
    ]
    # Nothing is tracked, so there is no series to fetch statistics for.
    assert [name for name, _ in fake.calls] == ["sonarr_search_series"]


def test_specials_can_be_requested(config: Config) -> None:
    """Season 0 is a real season, so request_series must accept it."""

    app = create_app(config)
    fake = FakeUpstream()
    fake.responses["sonarr_search_series"] = {
        "data": [
            {
                "tvdbId": 371980,
                "title": "Severance",
                "year": 2022,
                "id": 4,
                "seasons": [{"seasonNumber": 0}, {"seasonNumber": 1}],
            }
        ]
    }
    fake.responses["sonarr_get_series_by_id"] = {
        "data": {"id": 4, "seasons": [{"seasonNumber": 0}, {"seasonNumber": 1}]}
    }
    fake.responses["sonarr_get_episodes"] = {"data": [{"id": 55}]}
    with TestClient(app) as client:
        app.state.runtime.tools.upstream = fake
        response = client.post(
            "/api/tools/call",
            headers=_headers(),
            json={
                "actor": _actor(1001),
                "name": "request_series",
                "arguments": {"tvdb_id": 371980, "seasons": [0]},
            },
        )

    assert response.status_code == 200
    assert response.json()["result"]["series"]["seasons"] == [0]
    searched = [args for name, args in fake.calls if name == "sonarr_search_season"]
    assert searched == [{"seriesId": 4, "seasonNumber": 0}]


def test_request_series_still_rejects_a_negative_season(config: Config) -> None:
    app = create_app(config)
    fake = FakeUpstream()
    with TestClient(app) as client:
        app.state.runtime.tools.upstream = fake
        response = client.post(
            "/api/tools/call",
            headers=_headers(),
            json={
                "actor": _actor(1001),
                "name": "request_series",
                "arguments": {"tvdb_id": 371980, "seasons": [-1]},
            },
        )

    assert response.status_code == 400


def test_existing_series_enables_the_season_and_searches_it(config: Config) -> None:
    app = create_app(config)
    fake = FakeUpstream()
    fake.responses["sonarr_search_series"] = {
        "data": [
            {
                "id": 17,
                "tvdbId": 411959,
                "title": "3 Body Problem",
                "year": 2024,
                "seasons": [{"seasonNumber": 1, "monitored": False}],
            }
        ]
    }
    fake.responses["sonarr_get_series_by_id"] = {
        "id": 17,
        "title": "3 Body Problem",
        "path": "/data/tv/3 Body Problem",
        "seasons": [{"seasonNumber": 1, "monitored": False}],
    }
    fake.responses["sonarr_get_episodes"] = {"data": [{"id": 101}, {"id": 102}]}
    with TestClient(app) as client:
        app.state.runtime.tools.upstream = fake
        response = client.post(
            "/api/tools/call",
            headers=_headers(),
            json={
                "actor": _actor(1001),
                "name": "request_series",
                "arguments": {"tvdb_id": 411959, "seasons": [1]},
            },
        )
        assert response.status_code == 200, response.text
        update = next(arguments for name, arguments in fake.calls if name == "sonarr_update_series")
        assert update["series"]["seasons"][0]["monitored"] is True
        assert (
            "sonarr_update_episode_monitoring",
            {"episodeIds": [101, 102], "monitored": True},
        ) in fake.calls
        assert ("sonarr_search_season", {"seriesId": 17, "seasonNumber": 1}) in fake.calls


def test_plex_webhook_requires_capability_and_deduplicates(config: Config) -> None:
    app = create_app(config)
    payload = {
        "event": "library.new",
        "Metadata": {
            "type": "movie",
            "ratingKey": "42",
            "title": "A Movie",
            "slug": "a-movie",
            "Guid": [{"id": "tmdb://123"}],
        },
    }
    with TestClient(app) as client:
        assert client.post("/plex", json=payload).status_code == 401
        token = "plex-hook-secret-with-at-least-32-bytes"
        first = client.post(f"/plex?token={token}", json=payload)
        second = client.post(f"/plex?token={token}", json=payload)
        assert first.json() == {"accepted": True}
        assert second.json() == {"accepted": False}
        event = app.state.runtime.store.pending_media_events(int(time.time()) + 10)[0]
        assert event["plex_url"] == "https://watch.plex.tv/movie/a-movie"


def test_plex_webhook_accepts_existing_capability_path(config: Config) -> None:
    app = create_app(config)
    payload = {
        "event": "library.new",
        "Metadata": {
            "type": "movie",
            "ratingKey": "43",
            "title": "Another Movie",
            "Guid": [{"id": "tmdb://124"}],
        },
    }
    token = "plex-hook-secret-with-at-least-32-bytes"
    with TestClient(app) as client:
        response = client.post(f"/private/plex/{token}", json=payload)
        assert response.json() == {"accepted": True}


def test_plex_webhook_accepts_new_show(config: Config) -> None:
    app = create_app(config)
    payload = {
        "event": "library.new",
        "Metadata": {
            "type": "show",
            "ratingKey": "10537",
            "title": "3 Body Problem",
            "slug": "3-body-problem",
            "Guid": [{"id": "tvdb://411959"}],
        },
    }
    token = "plex-hook-secret-with-at-least-32-bytes"
    with TestClient(app) as client:
        response = client.post(f"/private/plex/{token}", json=payload)
        assert response.json() == {"accepted": True}
        event = app.state.runtime.store.pending_media_events(int(time.time()) + 10)[0]
        assert event["media_type"] == "series"
        assert event["external_id"] == 411959
        assert event["show_title"] == "3 Body Problem"
        assert event["plex_url"] == "https://watch.plex.tv/show/3-body-problem"


def test_plex_webhook_maps_season_parent_fields(config: Config) -> None:
    app = create_app(config)
    payload = {
        "event": "library.new",
        "Metadata": {
            "type": "season",
            "ratingKey": "10538",
            "title": "Season 1",
            "index": 1,
            "parentRatingKey": "10537",
            "parentTitle": "3 Body Problem",
            "parentGuid": "tvdb://411959",
            "parentSlug": "3-body-problem",
        },
    }
    token = "plex-hook-secret-with-at-least-32-bytes"
    with TestClient(app) as client:
        response = client.post(f"/private/plex/{token}", json=payload)
        assert response.json() == {"accepted": True}
        event = app.state.runtime.store.pending_media_events(int(time.time()) + 10)[0]
        assert event["external_id"] == 411959
        assert event["parent_rating_key"] == "10537"
        assert event["show_title"] == "3 Body Problem"
        assert event["season_number"] == 1
        assert event["plex_url"] == "https://watch.plex.tv/show/3-body-problem/season/1"


def test_plex_webhook_links_an_episode_to_the_mobile_app_route(config: Config) -> None:
    app = create_app(config)
    payload = {
        "event": "library.new",
        "Metadata": {
            "type": "episode",
            "ratingKey": "10539",
            "title": "Countdown",
            "index": 2,
            "parentIndex": 1,
            "grandparentRatingKey": "10537",
            "grandparentTitle": "3 Body Problem",
            "grandparentGuid": "tvdb://411959",
            "grandparentSlug": "3-body-problem",
        },
    }
    token = "plex-hook-secret-with-at-least-32-bytes"
    with TestClient(app) as client:
        response = client.post(f"/private/plex/{token}", json=payload)
        assert response.json() == {"accepted": True}
        event = app.state.runtime.store.pending_media_events(int(time.time()) + 10)[0]
        # watch.plex.tv, because only that form opens the Plex app.
        assert event["plex_url"] == ("https://watch.plex.tv/show/3-body-problem/season/1/episode/2")


def test_rejects_oversized_request_before_parsing(config: Config) -> None:
    app = create_app(config)
    with TestClient(app) as client:
        response = client.post(
            "/plex?token=plex-hook-secret-with-at-least-32-bytes",
            content=b"{}",
            headers={"Content-Length": str(5 * 1024 * 1024)},
        )
        assert response.status_code == 413


def test_rejects_oversized_stream_without_content_length(config: Config) -> None:
    app = create_app(config)
    chunks = (b"x" * 1024 * 1024 for _ in range(5))
    with TestClient(app) as client:
        response = client.post(
            "/plex?token=plex-hook-secret-with-at-least-32-bytes",
            content=chunks,
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 413


def test_blocked_actor_is_recorded_with_identity(config: Config) -> None:
    app = create_app(config)
    with TestClient(app) as client:
        response = client.post(
            "/api/actors",
            headers=_headers(),
            json={"actor": _actor(7777, username="blocked-person"), "blocked": False},
        )
        assert response.json()["role"] == "blocked"
        user = next(item for item in app.state.runtime.store.users({}) if item["user_id"] == 7777)
        assert user["username"] == "blocked-person"
        assert user["last_blocked"] is not None


async def test_notifications_group_bulk_season_and_reach_admin_and_requester(
    config: Config,
) -> None:
    app = create_app(config)
    sent: list[tuple[int, str, str]] = []
    with TestClient(app):
        runtime = app.state.runtime
        # Flushing a series batch consults the Sonarr queue; stub it so the
        # test does not reach for a real upstream.
        runtime.notifications.upstream = _idle_queue()
        runtime.store.record_request(
            media_type="series",
            external_id=411959,
            seasons=(1,),
            title="3 Body Problem",
            year=2024,
            actor=Actor(user_id=1001, chat_id=1001),
        )
        observed = int(time.time()) - 10
        for episode in (1, 2):
            runtime.store.add_media_event(
                event_key=f"episode:{episode}",
                media_type="series",
                external_id=411959,
                rating_key=str(episode),
                title=f"Episode {episode}",
                show_title="3 Body Problem",
                season_number=1,
                episode_number=episode,
                plex_url=f"https://app.plex.tv/{episode}",
                observed_at=observed,
            )

        async def capture(chat_id: int, text: str, url: str) -> None:
            sent.append((chat_id, text, url))

        runtime.notifications._send = capture  # type: ignore[method-assign]
        await runtime.notifications.flush()
        assert {item[0] for item in sent} == {1001, 9001}
        assert all("Season 1 (2 episodes)" in item[1] for item in sent)
        assert len(sent) == 2
        await runtime.notifications.flush()
        assert len(sent) == 2


async def test_automatic_plex_addition_notifies_admin_without_request(config: Config) -> None:
    app = create_app(config)
    sent: list[int] = []
    with TestClient(app):
        runtime = app.state.runtime
        runtime.store.add_media_event(
            event_key="movie:99",
            media_type="movie",
            external_id=999,
            rating_key="99",
            title="Automatic Import",
            show_title=None,
            season_number=None,
            episode_number=None,
            plex_url="https://app.plex.tv/99",
            observed_at=int(time.time()) - 10,
        )

        async def capture(chat_id: int, _text: str, _url: str) -> None:
            sent.append(chat_id)

        runtime.notifications._send = capture  # type: ignore[method-assign]
        await runtime.notifications.flush()
        assert sent == [9001]


async def test_notifications_filter_revoked_requesters_but_preserve_allowed_chat(
    config: Config,
) -> None:
    app = create_app(config)
    sent: list[int] = []
    with TestClient(app):
        runtime = app.state.runtime
        # Flushing a series batch consults the Sonarr queue; stub it so the
        # test does not reach for a real upstream.
        runtime.notifications.upstream = _idle_queue()
        runtime.store.record_request(
            media_type="movie",
            external_id=100,
            seasons=(),
            title="Allowed",
            year=2026,
            actor=Actor(user_id=1001, chat_id=-10001),
        )
        runtime.store.record_request(
            media_type="movie",
            external_id=200,
            seasons=(),
            title="Revoked",
            year=2026,
            actor=Actor(user_id=2002, chat_id=2002),
        )

        async def capture(chat_id: int, _text: str, _url: str) -> None:
            sent.append(chat_id)

        runtime.notifications._send = capture  # type: ignore[method-assign]
        for external_id, title in ((100, "Allowed"), (200, "Revoked")):
            runtime.store.add_media_event(
                event_key=f"movie:{external_id}",
                media_type="movie",
                external_id=external_id,
                rating_key=str(external_id),
                title=title,
                show_title=None,
                season_number=None,
                episode_number=None,
                plex_url=f"https://app.plex.tv/{external_id}",
                observed_at=int(time.time()) - 10,
            )

        await runtime.notifications.flush()

        assert sent.count(9001) == 2
        assert -10001 in sent
        assert 2002 not in sent


async def test_episode_enrichment_retries_before_requester_notification(config: Config) -> None:
    app = create_app(config)
    fake = FakeUpstream()
    attempts = 0

    def metadata(_arguments: dict[str, Any]) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary Plex lookup failure")
        return {"MediaContainer": {"Metadata": [{"Guid": [{"id": "tvdb://411959"}]}]}}

    fake.responses["plex_get_metadata"] = metadata
    sent: list[int] = []
    with TestClient(app):
        runtime = app.state.runtime
        runtime.notifications.upstream = fake
        runtime.store.record_request(
            media_type="series",
            external_id=411959,
            seasons=(1,),
            title="3 Body Problem",
            year=2024,
            actor=Actor(user_id=1001, chat_id=1001),
        )
        runtime.store.add_media_event(
            event_key="episode:retry",
            media_type="series",
            external_id=None,
            rating_key="100",
            title="Episode 1",
            show_title="3 Body Problem",
            season_number=1,
            episode_number=1,
            parent_rating_key="50",
            plex_url="https://app.plex.tv/100",
            observed_at=int(time.time()) - 10,
        )

        async def capture(chat_id: int, _text: str, _url: str) -> None:
            sent.append(chat_id)

        runtime.notifications._send = capture  # type: ignore[method-assign]
        await runtime.notifications.flush()
        assert sent == [9001]
        await runtime.notifications.flush()
        assert sent == [9001, 1001]


async def test_new_show_webhook_reaches_admin_and_requester(config: Config) -> None:
    app = create_app(config)
    sent: list[tuple[int, str]] = []
    with TestClient(app):
        runtime = app.state.runtime
        # Flushing a series batch consults the Sonarr queue; stub it so the
        # test does not reach for a real upstream.
        runtime.notifications.upstream = _idle_queue()
        runtime.store.record_request(
            media_type="series",
            external_id=411959,
            seasons=(1,),
            title="3 Body Problem",
            year=2024,
            actor=Actor(user_id=1001, chat_id=1001),
        )
        runtime.store.add_media_event(
            event_key="show:10537",
            media_type="series",
            external_id=411959,
            rating_key="10537",
            title="3 Body Problem",
            show_title="3 Body Problem",
            season_number=None,
            episode_number=None,
            plex_url="https://app.plex.tv/show",
            observed_at=int(time.time()) - 10,
        )

        async def capture(chat_id: int, text: str, _url: str) -> None:
            sent.append((chat_id, text))

        runtime.notifications._send = capture  # type: ignore[method-assign]
        await runtime.notifications.flush()
        assert {item[0] for item in sent} == {1001, 9001}
        assert all("Season 1" in item[1] for item in sent)


async def test_new_show_without_guid_enriches_from_its_own_rating_key(
    config: Config,
) -> None:
    app = create_app(config)
    fake = FakeUpstream()
    fake.responses["plex_get_metadata"] = {
        "MediaContainer": {"Metadata": {"Guid": [{"id": "tvdb://411959"}]}}
    }
    sent: list[int] = []
    with TestClient(app):
        runtime = app.state.runtime
        runtime.notifications.upstream = fake
        runtime.store.record_request(
            media_type="series",
            external_id=411959,
            seasons=(1,),
            title="3 Body Problem",
            year=2024,
            actor=Actor(user_id=1001, chat_id=1001),
        )
        runtime.store.add_media_event(
            event_key="show:10537",
            media_type="series",
            external_id=None,
            rating_key="10537",
            title="3 Body Problem",
            show_title="3 Body Problem",
            season_number=None,
            episode_number=None,
            plex_url="https://app.plex.tv/show",
            observed_at=int(time.time()) - 10,
        )

        async def capture(chat_id: int, _text: str, _url: str) -> None:
            sent.append(chat_id)

        runtime.notifications._send = capture  # type: ignore[method-assign]
        await runtime.notifications.flush()

        assert ("plex_get_metadata", {"ratingKey": "10537"}) in fake.calls
        assert set(sent) == {1001, 9001}


async def test_notification_enrichment_keeps_the_direct_server_link(config: Config) -> None:
    """Enrichment must not downgrade a server link to a catalogue page.

    It used to overwrite the stored link with a watch.plex.tv entry, which made
    the recipient choose a streaming service for a file already on the server.
    """

    app = create_app(config)
    with TestClient(app):
        runtime = app.state.runtime
        direct = (
            "https://app.plex.tv/desktop/#!/server/machine-123"
            "/details?key=%2Flibrary%2Fmetadata%2F19"
        )
        runtime.store.add_media_event(
            event_key="episode:19",
            media_type="series",
            external_id=81797,
            rating_key="19",
            title="Episode 19",
            show_title="One Piece",
            season_number=2,
            episode_number=19,
            plex_url=direct,
            observed_at=int(time.time()) - 10,
        )

        events = runtime.store.pending_media_events(int(time.time()) + 10)
        enriched = await runtime.notifications._enrich(events)

        assert enriched[0]["plex_url"] == direct
        assert runtime.store.pending_media_events(int(time.time()) + 10)[0]["plex_url"] == direct


async def test_notification_enrichment_upgrades_to_the_app_link(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stored server-route link is replaced once the slug can be resolved.

    The server route opens the browser client, so an event that fell back to it
    is upgraded to the watch.plex.tv form as soon as the id is known.
    """

    app = create_app(config)
    with TestClient(app):
        runtime = app.state.runtime
        runtime.store.add_media_event(
            event_key="episode:19",
            media_type="series",
            external_id=81797,
            rating_key="19",
            title="Episode 19",
            show_title="One Piece",
            season_number=2,
            episode_number=19,
            plex_url=(
                "https://app.plex.tv/desktop/#!/server/machine-123"
                "/details?key=%2Flibrary%2Fmetadata%2F19"
            ),
            observed_at=int(time.time()) - 10,
        )

        async def lookup(**_values: Any) -> str:
            return "one-piece"

        monkeypatch.setattr(plex_watch, "lookup_slug", lookup)
        events = runtime.store.pending_media_events(int(time.time()) + 10)
        enriched = await runtime.notifications._enrich(events)

        expected = "https://watch.plex.tv/show/one-piece/season/2/episode/19"
        assert enriched[0]["plex_url"] == expected
        assert runtime.store.pending_media_events(int(time.time()) + 10)[0]["plex_url"] == expected


async def test_plex_slug_lookup_keeps_token_out_of_the_url(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    config.upstream_token_file.write_text(
        "MCP_AUTH_TOKEN=upstream-secret\nPLEX_API_KEY=plex-secret\n", encoding="utf-8"
    )
    captured: dict[str, Any] = {}

    class Response:
        is_error = False

        @staticmethod
        def json() -> dict[str, Any]:
            return {"MediaContainer": {"Metadata": [{"slug": "one-piece"}]}}

    class Client:
        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, url: str, **values: Any) -> Response:
            captured.update({"url": url, **values})
            return Response()

    monkeypatch.setattr("media_gateway.plex_watch.httpx.AsyncClient", lambda **_kwargs: Client())
    slug = await plex_watch.lookup_slug(
        token_file=config.upstream_token_file, media_type="series", external_id=81797
    )

    assert slug == "one-piece"
    assert captured["params"] == {"guid": "tvdb://81797", "type": 2}
    assert "plex-secret" not in str(captured["url"])
    assert captured["headers"]["X-Plex-Token"] == "plex-secret"

    # The second ask is served from cache rather than repeating the request.
    captured.clear()
    assert (
        await plex_watch.lookup_slug(
            token_file=config.upstream_token_file, media_type="series", external_id=81797
        )
        == "one-piece"
    )
    assert captured == {}


def _quiet_series_event(runtime: Any, *, title: str = "Severance", tvdb: int = 371980) -> None:
    runtime.store.add_media_event(
        event_key=f"episode:{tvdb}",
        media_type="series",
        external_id=tvdb,
        rating_key=str(tvdb),
        title="Episode 1",
        show_title=title,
        season_number=1,
        episode_number=1,
        plex_url="https://watch.plex.tv/show/severance/season/1/episode/1",
        # Older than the quiet window, so only the queue can hold it back.
        observed_at=int(time.time()) - 600,
    )


async def test_a_quiet_season_waits_while_sonarr_is_still_fetching(config: Config) -> None:
    """An empty window is not proof the season finished arriving."""

    app = create_app(config)
    fake = FakeUpstream()
    fake.responses["sonarr_get_queue"] = {
        "data": {
            "records": [
                {
                    "seriesId": 4,
                    "seasonNumber": 1,
                    "series": {"tvdbId": 371980, "title": "Severance"},
                }
            ]
        }
    }
    sent: list[str] = []
    with TestClient(app):
        runtime = app.state.runtime
        runtime.upstream = fake
        runtime.notifications.upstream = fake

        async def send(chat_id: int, text: str, plex_url: str) -> None:
            sent.append(plex_url)

        runtime.notifications._send = send  # type: ignore[method-assign]
        _quiet_series_event(runtime)
        await runtime.notifications.flush()

    assert sent == []
    assert [e["event_key"] for e in runtime.store.pending_media_events(int(time.time()) + 10)] == [
        "episode:371980"
    ]


async def test_a_quiet_season_is_sent_once_the_queue_is_clear(config: Config) -> None:
    app = create_app(config)
    fake = FakeUpstream()
    fake.responses["sonarr_get_queue"] = {"data": {"records": []}}
    sent: list[str] = []
    with TestClient(app):
        runtime = app.state.runtime
        runtime.notifications.upstream = fake

        async def send(chat_id: int, text: str, plex_url: str) -> None:
            sent.append(plex_url)

        runtime.notifications._send = send  # type: ignore[method-assign]
        _quiet_series_event(runtime)
        await runtime.notifications.flush()

    assert sent == ["https://watch.plex.tv/show/severance/season/1/episode/1"]


async def test_a_different_show_in_the_queue_does_not_hold_this_one(config: Config) -> None:
    app = create_app(config)
    fake = FakeUpstream()
    fake.responses["sonarr_get_queue"] = {
        "data": {"records": [{"series": {"tvdbId": 81797, "title": "One Piece"}}]}
    }
    sent: list[str] = []
    with TestClient(app):
        runtime = app.state.runtime
        runtime.notifications.upstream = fake

        async def send(chat_id: int, text: str, plex_url: str) -> None:
            sent.append(plex_url)

        runtime.notifications._send = send  # type: ignore[method-assign]
        _quiet_series_event(runtime)
        await runtime.notifications.flush()

    assert len(sent) == 1


async def test_an_unreachable_sonarr_queue_still_delivers(config: Config) -> None:
    """A provider that cannot be reached must not hold notifications for ever."""

    app = create_app(config)

    class BrokenQueue(FakeUpstream):
        async def call(self, name: str, arguments: dict[str, Any]) -> Any:
            if name == "sonarr_get_queue":
                raise UpstreamError("sonarr is unavailable")
            return await super().call(name, arguments)

    sent: list[str] = []
    with TestClient(app):
        runtime = app.state.runtime
        runtime.notifications.upstream = BrokenQueue()

        async def send(chat_id: int, text: str, plex_url: str) -> None:
            sent.append(plex_url)

        runtime.notifications._send = send  # type: ignore[method-assign]
        _quiet_series_event(runtime)
        await runtime.notifications.flush()

    assert len(sent) == 1


async def test_a_movie_is_notified_without_waiting_for_the_batch_window(
    config: Config,
) -> None:
    """A movie is its own batch, so the season-batching delay buys it nothing."""

    app = create_app(config)
    sent: list[tuple[int, str]] = []
    with TestClient(app):
        runtime = app.state.runtime

        async def send(chat_id: int, text: str, plex_url: str) -> None:
            sent.append((chat_id, plex_url))

        runtime.notifications._send = send  # type: ignore[method-assign]
        runtime.store.add_media_event(
            event_key="movie:9001",
            media_type="movie",
            external_id=533535,
            rating_key="9001",
            title="Deadpool & Wolverine",
            show_title=None,
            season_number=None,
            episode_number=None,
            plex_url="https://watch.plex.tv/movie/deadpool-and-wolverine",
            observed_at=int(time.time()),
        )

        await runtime.notifications.flush()

    assert [url for _chat, url in sent] == ["https://watch.plex.tv/movie/deadpool-and-wolverine"]
    remaining = runtime.store.pending_media_events(int(time.time()) + 10)
    assert [event["event_key"] for event in remaining] == []


async def test_a_fresh_episode_still_waits_for_its_season(config: Config) -> None:
    """Series keep the window, or a season import fires one message per episode."""

    app = create_app(config)
    sent: list[str] = []
    with TestClient(app):
        runtime = app.state.runtime

        async def send(chat_id: int, text: str, plex_url: str) -> None:
            sent.append(plex_url)

        runtime.notifications._send = send  # type: ignore[method-assign]
        runtime.store.add_media_event(
            event_key="episode:9002",
            media_type="series",
            external_id=371980,
            rating_key="9002",
            title="Episode 1",
            show_title="Severance",
            season_number=1,
            episode_number=1,
            plex_url="https://watch.plex.tv/show/severance/season/1/episode/1",
            observed_at=int(time.time()),
        )

        await runtime.notifications.flush()

    assert sent == []
    remaining = runtime.store.pending_media_events(int(time.time()) + 10)
    assert [event["event_key"] for event in remaining] == ["episode:9002"]


async def test_season_batch_waits_until_the_latest_episode_is_quiet(config: Config) -> None:
    app = create_app(config)
    sent: list[int] = []
    with TestClient(app):
        runtime = app.state.runtime
        runtime.store.add_media_event(
            event_key="episode:old",
            media_type="series",
            external_id=411959,
            rating_key="1",
            title="Episode 1",
            show_title="3 Body Problem",
            season_number=1,
            episode_number=1,
            parent_rating_key="10537",
            plex_url="https://app.plex.tv/1",
            observed_at=int(time.time()) - 10,
        )
        runtime.store.add_media_event(
            event_key="episode:young",
            media_type="series",
            external_id=411959,
            rating_key="2",
            title="Episode 2",
            show_title="3 Body Problem",
            season_number=1,
            episode_number=2,
            parent_rating_key="10537",
            plex_url="https://app.plex.tv/2",
            observed_at=int(time.time()),
        )

        async def capture(chat_id: int, _text: str, _url: str) -> None:
            sent.append(chat_id)

        runtime.notifications._send = capture  # type: ignore[method-assign]
        await runtime.notifications.flush()
        assert sent == []


async def test_telegram_network_error_does_not_expose_bot_token(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(config)

    class FailingClient:
        async def __aenter__(self) -> FailingClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, url: str, **_kwargs: object) -> None:
            request = httpx.Request("POST", url)
            raise httpx.ConnectError("connection failed", request=request)

    monkeypatch.setattr(
        "media_gateway.notifications.httpx.AsyncClient",
        lambda **_kwargs: FailingClient(),
    )
    with TestClient(app), pytest.raises(RuntimeError) as captured:
        await app.state.runtime.notifications._send(9001, "Available", "https://app.plex.tv/item")
    assert "test-token" not in str(captured.value)


def test_request_status_shows_the_specific_provider_outcome() -> None:
    def status(state: str, provider_status: str | None) -> str:
        return _request_status({"state": state, "provider_status": provider_status})

    # An active acquisition must say which stage it is in; collapsing these
    # into one "Acquisition active" label is what made the old pair of
    # columns redundant.
    assert status("requested", "search_started") == "Searching for a release"
    assert status("requested", "awaiting_plex") == "Waiting for Plex"
    assert status("requested", "requested") == "Queued in Radarr/Sonarr"
    assert status("available", "available") == "Available"

    # Before the provider replies there is no outcome to show.
    assert status("pending", None) == "Intent pending"
    assert status("unknown", None) == "Needs reconciliation"

    # An unrecognized provider status is still rendered, never blanked.
    assert status("requested", "import_pending") == "Import Pending"


def test_dashboard_serves_the_brand_favicon(config: Config) -> None:
    with TestClient(create_app(config)) as client:
        response = client.get("/assets/favicon.svg")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("image/svg+xml")
        # The canonical CRBL mark: amber B on a dark rounded square.
        assert "#F59E0B" in response.text
        assert "#1C1917" in response.text

        login = client.get("/login")
        assert '<link rel="icon" href="/assets/favicon.svg" type="image/svg+xml">' in login.text
        # Same-origin only, so the strict default-src CSP still allows it.
        assert "default-src 'self'" in login.headers["content-security-policy"]


def test_request_table_keeps_status_on_one_line(config: Config) -> None:
    app = create_app(config)
    with TestClient(app) as client:
        runtime = app.state.runtime
        request_id = runtime.store.begin_request(
            media_type="movie",
            external_id=4242,
            seasons=(),
            title="The Boy Who Harnessed the Wind",
            year=2019,
            actor=Actor(user_id=1001, chat_id=1001),
        )
        runtime.store.complete_request(request_id, "search_started")
        _login(client)

        page = client.get("/")

        assert page.status_code == 200
        # Only the title may wrap; the status badge and the short columns stay
        # on one line so a long title cannot push them onto two.
        assert '<td class="nowrap"><span class="badge requested">' in page.text
        assert "Searching for a release" in page.text
        assert '<td class="cell-title">' in page.text
        assert "white-space:nowrap" in client.get("/assets/app.css").text


def test_the_flush_cycle_bounds_notification_latency() -> None:
    """The window cannot deliver sooner than the loop that tests it."""

    from media_gateway.notifications import FLUSH_INTERVAL_SECONDS

    assert FLUSH_INTERVAL_SECONDS <= 5
