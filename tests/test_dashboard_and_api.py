from __future__ import annotations

import json
import time
from typing import Any

from conftest import FakeUpstream
from starlette.testclient import TestClient

from media_gateway.app import COOKIE, create_app
from media_gateway.config import Config
from media_gateway.constants import ADMIN_UPSTREAM_TOOLS
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
        assert "#fffbf5" in client.get("/assets/app.css").text


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
        assert len(user_tools["tools"]) == 7
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
        assert len(tools) == 71
        assert {item["scope"] for item in tools} == {"shared", "admin"}
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
