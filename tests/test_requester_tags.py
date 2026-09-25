"""Requests are tagged in Radarr/Sonarr with the first name of who asked."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from conftest import FakeUpstream
from starlette.testclient import TestClient

from media_gateway.app import create_app
from media_gateway.config import Config
from media_gateway.tools import requester_tag_label
from media_gateway.upstream import Upstream, UpstreamError

HEADERS = {"Authorization": "Bearer gateway-secret-with-at-least-32-bytes"}


def _call(client: TestClient, name: str, arguments: dict[str, Any], **actor: Any) -> Any:
    return client.post(
        "/api/tools/call",
        headers=HEADERS,
        json={
            "actor": {"user_id": 1001, "chat_id": 1001, **actor},
            "name": name,
            "arguments": arguments,
        },
    )


def _movie(fake: FakeUpstream, *, radarr_id: int | None = None) -> None:
    item: dict[str, Any] = {"tmdbId": 123, "title": "A Movie", "year": 2026}
    if radarr_id is not None:
        item["id"] = radarr_id
        fake.responses["radarr_get_movie"] = {"id": radarr_id, "hasFile": False}
    fake.responses["radarr_search_movie"] = {"data": [item]}


@pytest.mark.parametrize(
    ("first_name", "username", "expected"),
    [
        ("Katy", "cakesmom", "katy"),
        ("Zoë Ann", None, "zoe-ann"),
        ("出各类账号", "some_user", "some-user"),
        (None, "goyedv", "goyedv"),
        ("出各类账号", None, None),
        (None, None, None),
    ],
)
def test_tag_label_is_the_first_name_in_arr_safe_form(
    first_name: str | None, username: str | None, expected: str | None
) -> None:
    assert requester_tag_label(first_name, username) == expected


def test_new_movie_carries_the_requester_tag(config: Config) -> None:
    app = create_app(config)
    fake = FakeUpstream()
    _movie(fake)
    with TestClient(app) as client:
        app.state.runtime.tools.upstream = fake
        response = _call(client, "request_movie", {"tmdb_id": 123}, first_name="Katy")

    assert response.status_code == 200, response.text
    assert ("ensure_tag", {"service": "radarr", "label": "katy"}) in fake.calls
    added = next(args for name, args in fake.calls if name == "radarr_add_movie")
    # The configured tags stay; the requester's joins them.
    assert added["tags"] == [3, 100]


def test_rerequested_movie_gains_the_requester_tag(config: Config) -> None:
    app = create_app(config)
    fake = FakeUpstream()
    _movie(fake, radarr_id=55)
    with TestClient(app) as client:
        app.state.runtime.tools.upstream = fake
        response = _call(client, "request_movie", {"tmdb_id": 123}, first_name="Katy")

    assert response.status_code == 200, response.text
    assert response.json()["result"]["status"] == "search_started"
    assert ("add_tags", {"service": "radarr", "id": 55, "tags": [100]}) in fake.calls


def test_tagging_failure_never_fails_the_request(config: Config) -> None:
    app = create_app(config)
    fake = FakeUpstream()
    _movie(fake)
    fake.responses["ensure_tag"] = UpstreamError("Radarr tags are unavailable")
    with TestClient(app) as client:
        app.state.runtime.tools.upstream = fake
        response = _call(client, "request_movie", {"tmdb_id": 123}, first_name="Katy")

    assert response.status_code == 200, response.text
    added = next(args for name, args in fake.calls if name == "radarr_add_movie")
    assert added["tags"] == [3]


def test_a_user_without_a_usable_name_gets_no_tag(config: Config) -> None:
    app = create_app(config)
    fake = FakeUpstream()
    _movie(fake)
    with TestClient(app) as client:
        app.state.runtime.tools.upstream = fake
        response = _call(client, "request_movie", {"tmdb_id": 123})

    assert response.status_code == 200, response.text
    assert not [call for call in fake.calls if call[0] == "ensure_tag"]


def test_new_series_carries_the_requester_tag(config: Config) -> None:
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
    with TestClient(app) as client:
        app.state.runtime.tools.upstream = fake
        response = _call(
            client, "request_series", {"tvdb_id": 411959, "seasons": [1]}, first_name="DV"
        )

    assert response.status_code == 200, response.text
    assert ("ensure_tag", {"service": "sonarr", "label": "dv"}) in fake.calls
    added = next(args for name, args in fake.calls if name == "sonarr_add_series")
    assert added["tags"] == [4, 100]


@pytest.mark.parametrize("exists", [True, False])
async def test_ensure_tag_reuses_or_creates_the_label(
    config: Config, monkeypatch: pytest.MonkeyPatch, exists: bool
) -> None:
    monkeypatch.setattr(
        "media_gateway.upstream.read_dotenv",
        lambda *_args: {"SONARR_URL": "http://sonarr:8989", "SONARR_API_KEY": "k" * 32},
    )
    posted: list[object] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Api-Key"] == "k" * 32
        assert request.url.path == "/api/v3/tag"
        if request.method == "GET":
            tags = [{"id": 3, "label": "trakt_trending"}]
            if exists:
                tags.append({"id": 9, "label": "Katy"})
            return httpx.Response(200, json=tags)
        posted.append(request.read())
        return httpx.Response(201, json={"id": 14, "label": "katy"})

    client = httpx.AsyncClient
    monkeypatch.setattr(
        "media_gateway.upstream.httpx.AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs),
    )
    upstream = Upstream(config.upstream_url, config.upstream_token_file)
    assert await upstream.ensure_tag("sonarr", "katy") == (9 if exists else 14)
    assert posted == ([] if exists else [b'{"label":"katy"}'])


async def test_add_tags_keeps_existing_tags(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "media_gateway.upstream.read_dotenv",
        lambda *_args: {"RADARR_URL": "http://radarr:7878", "RADARR_API_KEY": "k" * 32},
    )
    seen: list[tuple[str, str, bytes]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.read()))
        return httpx.Response(202, json=[])

    client = httpx.AsyncClient
    monkeypatch.setattr(
        "media_gateway.upstream.httpx.AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs),
    )
    await Upstream(config.upstream_url, config.upstream_token_file).add_tags("radarr", 55, [14])
    assert seen == [
        ("PUT", "/api/v3/movie/editor", b'{"movieIds":[55],"tags":[14],"applyTags":"add"}')
    ]
