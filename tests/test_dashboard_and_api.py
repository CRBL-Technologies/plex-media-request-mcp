from __future__ import annotations

import json
import time
from typing import Any

import httpx
import pytest
from conftest import FakeUpstream
from starlette.testclient import TestClient

from media_gateway.app import COOKIE, create_app
from media_gateway.config import Config
from media_gateway.constants import ADMIN_UPSTREAM_TOOLS, SHARED_TOOLS
from media_gateway.dashboard import _request_status
from media_gateway.types import Actor
from media_gateway.upstream import UpstreamError


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
                    "titles": ["Arrival (2016)", "Ex Machina (2014)", "Annihilation (2018)"],
                    "media_type": "movie",
                },
            },
        )

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["presentation"] == "recommendations"
    assert [item["tmdb_id"] for item in result["results"]] == [11, 12, 13]
    assert [call[1]["term"] for call in fake.calls] == [
        "Arrival (2016)",
        "Ex Machina (2014)",
        "Annihilation (2018)",
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
                    "titles": ["Arrival (2016)", "Arrival (2025)"],
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
                    "titles": ["Arrival (2016)", "Ex Machina (2014)"],
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


async def test_notification_enrichment_persists_universal_plex_link(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
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
            plex_url="https://app.plex.tv/fallback",
            observed_at=int(time.time()) - 10,
        )

        async def lookup(_media_type: str, _external_id: int) -> str:
            return "one-piece"

        monkeypatch.setattr(runtime.notifications, "_lookup_plex_slug", lookup)
        events = runtime.store.pending_media_events(int(time.time()) + 10)
        enriched = await runtime.notifications._enrich(events)

        expected = "https://watch.plex.tv/show/one-piece/season/2/episode/19"
        assert enriched[0]["plex_url"] == expected
        persisted = runtime.store.pending_media_events(int(time.time()) + 10)
        assert persisted[0]["plex_url"] == expected


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

    monkeypatch.setattr("media_gateway.notifications.httpx.AsyncClient", lambda **_kwargs: Client())
    app = create_app(config)
    with TestClient(app):
        slug = await app.state.runtime.notifications._lookup_plex_slug("series", 81797)

    assert slug == "one-piece"
    assert captured["params"] == {"guid": "tvdb://81797", "type": 2}
    assert "plex-secret" not in str(captured["url"])
    assert captured["headers"]["X-Plex-Token"] == "plex-secret"


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
