from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from media_companion.enumeration import (
    EnumerationError,
    EpisodeState,
    diff_enumerations,
    enumerate_episodes,
)
from media_companion.plex_ingress import (
    MAX_BODY_BYTES,
    WebhookCapabilityError,
    WebhookLimitError,
    WebhookValidationError,
    canonical_rating_key,
    metadata_path,
    parse_plex_webhook,
)
from media_companion.resolver import (
    ResolutionStatus,
    TombstoneTracker,
    extract_provider_ids,
    resolve_provider_identity,
)


def _multipart(
    payload: dict[str, object], *, poster: bytes | None = None
) -> tuple[bytes, str]:
    boundary = "----codex-plex-test"
    chunks = [
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="payload"\r\n'
            "Content-Type: application/json\r\n\r\n"
        ).encode()
        + json.dumps(payload, separators=(",", ":")).encode()
    ]
    if poster is not None:
        chunks.append(
            (
                f"\r\n--{boundary}\r\n"
                'Content-Disposition: form-data; name="thumb"; filename="../../ignored.jpg"\r\n'
                "Content-Type: image/jpeg\r\n\r\n"
            ).encode()
            + poster
        )
    chunks.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _movie_payload(**metadata: object) -> dict[str, object]:
    return {
        "event": "library.new",
        "server": {"uuid": "server-1", "machineIdentifier": "machine-1"},
        "metadata": {
            "type": "movie",
            "librarySectionID": 1,
            "librarySectionTitle": "Movies",
            "ratingKey": "123",
            "title": "Example",
            "guid": "tmdb://42",
            **metadata,
        },
    }


def test_ingress_bounds_capability_rating_key_and_sanitized_event() -> None:
    capability = "A" * 43
    body, content_type = _multipart(_movie_payload(title="Example\n"), poster=b"jpeg")
    event = parse_plex_webhook(
        body,
        content_type,
        request_path=f"/private/plex/{capability}",
        capability=capability,
        expected_server_uuid="server-1",
        allowed_library_ids={"1"},
    )
    assert event is not None
    assert event.title == "Example"
    assert event.poster_size == 4
    assert "payload" not in event.sanitized_dict()
    assert "jpeg" not in event.sanitized_json()
    assert event.to_record()["event_key"] == (
        '{"library_uuid":"1","rating_key":"123","server_uuid":"server-1",'
        '"tombstone_generation":0,"version":1}'
    )
    child_body, child_type = _multipart(
        _movie_payload(type="episode", ratingKey="124", parentIndex=1, index=1)
    )
    child = parse_plex_webhook(
        child_body,
        child_type,
        request_path=f"/private/plex/{capability}",
        capability=capability,
        expected_server_uuid="server-1",
        allowed_library_ids={"1"},
    )
    assert child is not None and child.grandparent_rating_key is None
    assert metadata_path("123") == "/library/metadata/123"
    for value in ("0", "001", "1/2", "1?secret", "../1"):
        with pytest.raises(WebhookValidationError):
            canonical_rating_key(value)


def test_ingress_rejects_wrong_capability_and_oversized_body() -> None:
    body, content_type = _multipart(_movie_payload())
    with pytest.raises(WebhookCapabilityError):
        parse_plex_webhook(
            body,
            content_type,
            request_path="/private/plex/wrong",
            capability="A" * 43,
            expected_server_uuid="server-1",
            allowed_library_ids={"1"},
        )
    with pytest.raises(WebhookLimitError):
        parse_plex_webhook(
            b"x" * (MAX_BODY_BYTES + 1),
            content_type,
            request_path="/private/plex/" + "A" * 43,
            capability="A" * 43,
            expected_server_uuid="server-1",
            allowed_library_ids={"1"},
        )


def test_provider_resolution_requires_ids_and_rejects_conflict_without_titles() -> None:
    ids = extract_provider_ids(
        {"type": "movie", "title": "Same title", "guid": "tmdb://42"}
    )
    assert ids.tmdb_id == 42
    assert (
        resolve_provider_identity({"type": "movie", "title": "Same title"}).status
        is ResolutionStatus.UNRESOLVED
    )
    conflict = resolve_provider_identity(
        {"type": "movie", "guid": "tmdb://42", "tmdbId": 43}
    )
    assert conflict.status is ResolutionStatus.CONFLICT
    imdb = extract_provider_ids({"guid": "com.plexapp.agents.imdb://tt0111161"})
    assert imdb.imdb_id == "tt0111161"
    malformed = extract_provider_ids({"guids": ["tmdb://42", "tmdb://000042"]})
    assert malformed.conflicted


def test_tombstone_reuse_requires_later_verified_added_at() -> None:
    tracker = TombstoneTracker()
    first_time = datetime(2026, 8, 1, tzinfo=timezone.utc)
    second_time = datetime(2026, 8, 2, tzinfo=timezone.utc)
    first = tracker.observe(
        {
            "server_uuid": "s",
            "library_uuid": "l",
            "rating_key": "7",
            "added_at": first_time,
        },
        observed_at=first_time,
    )
    assert first.generation.generation == 0
    tracker.mark_deleted("s", "l", "7", deleted_at=second_time)
    reused = tracker.observe(
        {
            "server_uuid": "s",
            "library_uuid": "l",
            "rating_key": "7",
            "added_at": second_time,
        },
        observed_at=second_time,
    )
    assert reused.is_new_lifecycle
    assert reused.generation.generation == 1
    tracker.mark_deleted("s", "l", "7", deleted_at=second_time)
    ambiguous = tracker.observe(
        {"server_uuid": "s", "library_uuid": "l", "rating_key": "7"},
        observed_at=second_time,
    )
    assert ambiguous.quarantined


def test_episode_enumeration_specials_tba_cancelled_and_late_episode() -> None:
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    with pytest.raises(EnumerationError):
        enumerate_episodes([], 0, provider_id="10")
    baseline = enumerate_episodes(
        [
            {
                "id": 1,
                "seasonNumber": 0,
                "episodeNumber": 1,
                "airDate": "2026-01-01",
                "hasFile": True,
            },
            {"id": 2, "seasonNumber": 0, "episodeNumber": 2, "status": "TBA"},
            {"id": 3, "seasonNumber": 0, "episodeNumber": 3, "status": "Canceled"},
        ],
        0,
        provider_id="10",
        requested_explicitly=True,
        expected_count=3,
        authoritative=True,
        season_ended=False,
        now=now,
    )
    assert baseline.mode.value == "airing_episode"
    assert [episode.state for episode in baseline.episodes] == [
        EpisodeState.AVAILABLE,
        EpisodeState.TBA,
        EpisodeState.CANCELED,
    ]
    assert len(baseline.available) == 1
    assert len(baseline.future) == 1
    assert len(baseline.required) == 2
    later = enumerate_episodes(
        [
            {
                "id": 1,
                "seasonNumber": 0,
                "episodeNumber": 1,
                "airDate": "2026-01-01",
                "hasFile": True,
            },
            {
                "id": 2,
                "seasonNumber": 0,
                "episodeNumber": 2,
                "airDate": "2026-08-10",
                "hasFile": True,
            },
            {"id": 3, "seasonNumber": 0, "episodeNumber": 3, "status": "Canceled"},
            {
                "id": 4,
                "seasonNumber": 0,
                "episodeNumber": 4,
                "airDate": "2026-08-11",
                "hasFile": True,
            },
        ],
        0,
        provider_id="10",
        requested_explicitly=True,
        expected_count=4,
        authoritative=True,
        season_ended=True,
        now=now,
        version=2,
    )
    delta = diff_enumerations(baseline, later, completion_sent=True)
    assert [episode.provider_id for episode in delta.late] == ["4"]
    assert delta.new_generation
