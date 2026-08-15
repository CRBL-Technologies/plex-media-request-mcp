from __future__ import annotations

import io
import json
import math
from collections.abc import Mapping

import pytest

from media_companion.clients.plex import (
    AdapterResponseError,
    HTTPResponse,
    PlexClient,
    PlexLibrary,
)
from media_companion.models import MediaIdentity, MediaType
from media_companion.plex_ingress import (
    WebhookLimitError,
    parse_multipart,
    parse_plex_webhook,
    read_bounded_body,
    structured_plex_event_key,
)
from media_companion.resolver import (
    ProviderIds,
    TombstoneGeneration,
    extract_provider_ids,
)
from media_companion.safe_views import sanitize_library_item


class FakeTransport:
    def __init__(self, responses: list[HTTPResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, Mapping[str, object]]] = []

    def request(self, method: str, url: str, **kwargs: object) -> HTTPResponse:
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected Plex request")
        return self.responses.pop(0)


def _multipart(payload: Mapping[str, object]) -> tuple[bytes, str]:
    boundary = "----plex-remediation-test"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="payload"\r\n'
        "Content-Type: application/json\r\n\r\n"
        f"{json.dumps(payload, separators=(',', ':'))}\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    return body, f"multipart/form-data; boundary={boundary}"


def _metadata(
    rating_key: str,
    *,
    tmdb_id: int = 42,
    tvdb_id: int | None = None,
    library_fields: Mapping[str, object] | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "ratingKey": rating_key,
        "type": "movie",
        "title": f"Movie {rating_key}",
        "Guid": [{"id": f"tmdb://{tmdb_id}"}],
        # Plex's initial webhook/search envelope is not enough to prove
        # availability; the re-fetch fixture carries an actual Part/file.
        "Media": [{"Part": [{"file": f"/library/{rating_key}.mkv"}]}],
    }
    if tvdb_id is not None:
        value["tvdbId"] = tvdb_id
    value.update(library_fields or {})
    return value


def _response(value: object) -> HTTPResponse:
    return HTTPResponse(
        200,
        {"content-type": "application/json"},
        json.dumps(value, separators=(",", ":")).encode(),
    )


def test_deadline_rejects_nan_and_infinity() -> None:
    payload = {
        "event": "library.new",
        "server": {"uuid": "server-1"},
        "metadata": {
            "type": "movie",
            "librarySectionID": 1,
            "librarySectionTitle": "Movies",
            "ratingKey": "1",
            "title": "Movie",
        },
    }
    body, content_type = _multipart(payload)
    for deadline in (math.nan, math.inf, -math.inf):
        with pytest.raises(WebhookLimitError):
            parse_plex_webhook(
                body,
                content_type,
                request_path="/private/plex/" + "A" * 43,
                capability="A" * 43,
                expected_server_uuid="server-1",
                allowed_library_ids={"1"},
                deadline_seconds=deadline,
            )
        with pytest.raises(WebhookLimitError):
            read_bounded_body(io.BytesIO(body), deadline_seconds=deadline)
        with pytest.raises(WebhookLimitError):
            parse_multipart(body, content_type, deadline=deadline)


def test_structured_event_keys_do_not_collide_on_colons() -> None:
    first = structured_plex_event_key("a:b", "c", "1", 0)
    second = structured_plex_event_key("a", "b:c", "1", 0)
    assert first != second
    assert (
        TombstoneGeneration("a:b", "c", "1").storage_key
        != TombstoneGeneration("a", "b:c", "1").storage_key
    )


def test_resolver_exposes_overlong_guid_as_conflict() -> None:
    ids = extract_provider_ids({"guid": "tmdb://" + "7" * 2_000})
    assert ids.conflicted
    assert "guid_invalid" in ids.conflicts
    malformed = extract_provider_ids({"Guid": [{"id": 42}]})
    assert malformed.conflicted
    assert "guid_invalid" in malformed.conflicts
    direct = ProviderIds(source_guids=("x" * 2_000,))
    assert direct.conflicted
    assert direct.source_guids == ()


def test_plex_metadata_accepts_real_section_fields_and_validates_link() -> None:
    transport = FakeTransport(
        [
            _response(
                {
                    "MediaContainer": {
                        "Metadata": [
                            _metadata(
                                "1",
                                library_fields={
                                    "librarySectionID": "1",
                                    "librarySectionTitle": "Movies",
                                },
                            )
                        ]
                    }
                }
            )
        ]
    )
    client = PlexClient(
        "https://plex.example",
        "secret",
        server_uuid="server-1",
        machine_identifier="machine-1",
        allowed_library_keys=("1",),
        transport=transport,
    )
    item = client.get_metadata("1").item
    assert item.library_key == "1"
    assert item.library_name == "Movies"
    assert item.plex_url is not None
    assert "secret" not in item.plex_url
    safe = sanitize_library_item(item)
    assert safe.plex_url == item.plex_url


def test_plex_metadata_requires_playable_part_then_accepts_complete_snapshot() -> None:
    incomplete = _metadata("1")
    incomplete["Media"] = [{"Part": []}]
    complete = _metadata("1")
    transport = FakeTransport(
        [
            _response(
                {
                    "MediaContainer": {
                        "Metadata": [
                            incomplete
                            | {
                                "librarySectionID": "1",
                                "librarySectionTitle": "Movies",
                            }
                        ]
                    }
                }
            ),
            _response(
                {
                    "MediaContainer": {
                        "Metadata": [
                            complete
                            | {
                                "librarySectionID": "1",
                                "librarySectionTitle": "Movies",
                            }
                        ]
                    }
                }
            ),
        ]
    )
    client = PlexClient(
        "https://plex.example",
        "secret",
        server_uuid="server-1",
        allowed_library_keys=("1",),
        transport=transport,
    )
    with pytest.raises(AdapterResponseError):
        client.get_metadata("1")
    metadata = client.get_metadata("1")
    assert metadata.snapshot_verified
    assert metadata.playable
    assert metadata.snapshot is not None
    assert metadata.snapshot.media_count == 1
    assert metadata.snapshot.part_count == 1
    assert metadata.snapshot.file_count == 1


def test_plex_metadata_rejects_title_only_scope() -> None:
    transport = FakeTransport(
        [
            _response(
                {
                    "MediaContainer": {
                        "Metadata": [
                            _metadata(
                                "1",
                                library_fields={"librarySectionTitle": "Movies"},
                            )
                        ]
                    }
                }
            )
        ]
    )
    client = PlexClient(
        "https://plex.example",
        "secret",
        server_uuid="server-1",
        allowed_library_names=("Movies",),
        transport=transport,
    )
    with pytest.raises(AdapterResponseError):
        client.get_metadata("1")


def test_plex_identity_matching_rejects_shared_namespace_conflict() -> None:
    transport = FakeTransport(
        [
            _response(
                {
                    "MediaContainer": {
                        "Hub": [
                            {
                                "type": "movie",
                                "Metadata": [
                                    _metadata(
                                        "1",
                                        tvdb_id=8,
                                        library_fields={
                                            "librarySectionID": "1",
                                            "librarySectionTitle": "Movies",
                                        },
                                    )
                                ],
                            }
                        ]
                    }
                }
            )
        ]
    )
    client = PlexClient(
        "https://plex.example",
        "secret",
        server_uuid="server-1",
        allowed_library_keys=("1",),
        transport=transport,
    )
    expected = MediaIdentity(MediaType.MOVIE, tmdb_id=42, tvdb_id=7)
    assert not client.find_metadata_by_identity(expected).items


def test_library_sweep_uses_checked_container_pagination() -> None:
    first = {
        "MediaContainer": {
            "offset": 0,
            "size": 2,
            "totalSize": 3,
            "Metadata": [_metadata("1"), _metadata("2")],
        }
    }
    second = {
        "MediaContainer": {
            "offset": 2,
            "size": 1,
            "totalSize": 3,
            "Metadata": [_metadata("3")],
        }
    }
    transport = FakeTransport([_response(first), _response(second)])
    client = PlexClient(
        "https://plex.example",
        "secret",
        server_uuid="server-1",
        allowed_library_keys=("1",),
        transport=transport,
    )
    items = tuple(
        client.iter_library_items(PlexLibrary("1", "Movies", "movie"), page_size=2)
    )
    assert [item.rating_key for item in items] == ["1", "2", "3"]
    assert transport.calls[0][2]["params"]["X-Plex-Container-Start"] == 0  # type: ignore[index]
    assert transport.calls[1][2]["params"]["X-Plex-Container-Start"] == 2  # type: ignore[index]


def test_library_sweep_rejects_inconsistent_offset() -> None:
    transport = FakeTransport(
        [
            _response(
                {
                    "MediaContainer": {
                        "offset": 1,
                        "totalSize": 1,
                        "Metadata": [_metadata("1")],
                    }
                }
            )
        ]
    )
    client = PlexClient(
        "https://plex.example",
        "secret",
        server_uuid="server-1",
        allowed_library_keys=("1",),
        transport=transport,
    )
    with pytest.raises(AdapterResponseError):
        tuple(client.iter_library_items(PlexLibrary("1", "Movies", "movie")))


def test_library_sweep_rejects_repeated_raw_rating_key_even_if_one_is_incomplete() -> (
    None
):
    first = _metadata("1")
    second = _metadata("1")
    second["Media"] = [{"Part": []}]
    transport = FakeTransport(
        [
            _response(
                {
                    "MediaContainer": {
                        "offset": 0,
                        "totalSize": 2,
                        "Metadata": [first, second],
                    }
                }
            )
        ]
    )
    client = PlexClient(
        "https://plex.example",
        "secret",
        server_uuid="server-1",
        allowed_library_keys=("1",),
        transport=transport,
    )
    with pytest.raises(AdapterResponseError):
        tuple(
            client.iter_library_items(PlexLibrary("1", "Movies", "movie"), page_size=2)
        )
