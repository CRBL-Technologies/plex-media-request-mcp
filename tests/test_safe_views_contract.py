"""Focused contract tests for the bounded shared-view primitives."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from media_companion.cursors import (
    CursorBindingError,
    CursorExpired,
    CursorSigner,
    InvalidCursor,
    SnapshotStore,
)
from media_companion.models import (
    MediaCandidate,
    MediaIdentity,
    MediaStatus,
    MediaType,
    PartialError,
    QueueItem,
    QueueState,
    ServiceName,
)
from media_companion.operations import safe_operation_result
from media_companion.rate_limit import (
    ADMIN_EXECUTION_LIMIT,
    ADMIN_PREVIEW_LIMIT,
    SAFE_REQUEST_USER_LIMIT,
    SHARED_READ_USER_LIMIT,
    RateLimiter,
    RateLimitPolicy,
)
from media_companion.safe_views import (
    MAX_RESPONSE_BYTES,
    MAX_SNAPSHOT_ITEMS,
    PageSizeError,
    ResponseTooLargeError,
    SafePage,
    SafeLibraryItem,
    SafeRequestStatus,
    SafeServiceHealth,
    SafeViewError,
    SafeViewPaginator,
    UnsafeProviderDataError,
    bounded_page,
    build_plex_link,
    sanitize_queue_item,
    normalize_page_size,
    sanitize_library_item,
    serialize_response,
    serialize_record,
)


class RateLimitContractTests(unittest.TestCase):
    def test_frozen_ceilings_cannot_be_raised(self) -> None:
        with self.assertRaises(ValueError):
            RateLimitPolicy(shared_read_user_limit=SHARED_READ_USER_LIMIT + 1)
        with self.assertRaises(ValueError):
            RateLimitPolicy(safe_request_window_seconds=601)

    def test_shared_and_safe_budgets_are_atomic_across_scopes(self) -> None:
        limiter = RateLimiter()
        for index in range(SHARED_READ_USER_LIMIT):
            self.assertTrue(
                limiter.allow("shared_read", user_id=7, chat_id=9, now=float(index))
            )
        # The user scope is full, but the chat/global scopes must not have
        # consumed an extra unit when this call is denied.
        self.assertFalse(limiter.allow("shared_read", user_id=7, chat_id=9, now=29))
        global_limiter = RateLimiter()
        for user in range(8):
            for index in range(SHARED_READ_USER_LIMIT):
                self.assertTrue(
                    global_limiter.allow(
                        "shared_read",
                        user_id=100 + user,
                        chat_id=10 + user,
                        now=float(index),
                    )
                )
        self.assertFalse(
            global_limiter.allow("shared_read", user_id=999, chat_id=999, now=29)
        )

        safe = RateLimiter()
        for index in range(SAFE_REQUEST_USER_LIMIT):
            self.assertTrue(
                safe.allow("safe_request", user_id=7, chat_id=9, now=float(index))
            )
        self.assertFalse(safe.allow("safe_request", user_id=7, chat_id=9, now=4))

    def test_admin_preview_and_execution_have_frozen_global_budgets(self) -> None:
        limiter = RateLimiter()
        for index in range(ADMIN_PREVIEW_LIMIT):
            self.assertTrue(limiter.allow("admin_preview", now=float(index)))
        self.assertFalse(limiter.allow("admin_preview", now=19))
        for index in range(ADMIN_EXECUTION_LIMIT):
            self.assertTrue(limiter.allow("admin_execution", now=float(index)))
        self.assertFalse(limiter.allow("dashboard_recovery", now=4))

    def test_denied_new_identities_do_not_grow_buckets(self) -> None:
        policy = RateLimitPolicy(
            shared_read_user_limit=1,
            shared_read_chat_limit=1,
            shared_read_global_limit=1,
        )
        limiter = RateLimiter(policy)
        self.assertTrue(limiter.allow("shared_read", user_id=1, chat_id=1, now=1))
        before = limiter.bucket_count
        for identity in range(2, 200):
            self.assertFalse(
                limiter.allow(
                    "shared_read",
                    user_id=identity,
                    chat_id=identity,
                    now=1,
                )
            )
        self.assertEqual(limiter.bucket_count, before)

    def test_bucket_capacity_fails_closed_without_partial_consumption(self) -> None:
        limiter = RateLimiter(max_buckets=3)
        self.assertTrue(limiter.allow("shared_read", user_id=1, chat_id=1, now=1))
        self.assertEqual(limiter.bucket_count, 3)
        self.assertFalse(limiter.allow("shared_read", user_id=2, chat_id=2, now=1))
        self.assertEqual(limiter.bucket_count, 3)


class CursorContractTests(unittest.TestCase):
    def test_cursor_is_signed_actor_and_filter_bound_for_five_minutes(self) -> None:
        signer = CursorSigner(b"cursor-key")
        token = signer.issue(
            user_id=42,
            chat_id=-42,
            tool="search_media",
            query={"query": "Dune"},
            snapshot_id="ABCDEFGHIJKLMNOPQRSTUVWX",
            now=100,
        )
        claims = signer.verify(
            token,
            user_id=42,
            chat_id=-42,
            expected_tool="search_media",
            query={"query": "Dune"},
            now=399,
        )
        self.assertEqual(claims.offset, 0)
        with self.assertRaises(CursorBindingError):
            signer.verify(token, user_id=43, chat_id=-42, now=100)
        with self.assertRaises(CursorBindingError):
            signer.verify(
                token, user_id=42, chat_id=-42, query={"query": "Alien"}, now=100
            )
        with self.assertRaises(CursorExpired):
            signer.verify(token, user_id=42, chat_id=-42, now=400)
        with self.assertRaises(InvalidCursor):
            signer.verify(token[:-1] + ("A" if token[-1] != "A" else "B"), now=100)

    def test_snapshot_store_caps_items_and_binds_cursor(self) -> None:
        signer = CursorSigner(b"snapshot-key")
        store = SnapshotStore(signer)
        snapshot = store.create(
            range(MAX_SNAPSHOT_ITEMS + 1),
            user_id=1,
            chat_id=2,
            tool="browse_library",
            query={"media_type": "movie"},
            now=10,
        )
        self.assertEqual(len(snapshot.items), MAX_SNAPSHOT_ITEMS)
        self.assertTrue(snapshot.truncated)
        cursor = store.cursor(snapshot, offset=25, page_size=25, now=10)
        resolved = store.get(
            cursor,
            user_id=1,
            chat_id=2,
            expected_tool="browse_library",
            query={"media_type": "movie"},
            now=11,
        )
        self.assertEqual(resolved.items[25], 25)

    def test_cursor_verification_requires_complete_context(self) -> None:
        signer = CursorSigner(b"strict-key")
        token = signer.issue(
            user_id=1,
            chat_id=2,
            tool="search_media",
            query=None,
            snapshot_id="ABCDEFGHIJKLMNOPQRSTUVWX",
            now=10,
        )
        with self.assertRaises(CursorBindingError):
            signer.verify(token, now=10)
        with self.assertRaises(CursorBindingError):
            signer.verify(token, user_id=1, chat_id=2, now=10)

    def test_snapshot_registry_is_bounded_and_partial_errors_are_bounded(self) -> None:
        signer = CursorSigner(b"registry-key")
        store = SnapshotStore(signer, max_records=2)
        for index in range(3):
            store.create(
                [index],
                user_id=1,
                chat_id=2,
                tool="search_media",
                now=index + 1,
            )
        self.assertEqual(len(store), 2)
        with self.assertRaises(ValueError):
            store.create(
                [1],
                user_id=1,
                chat_id=2,
                tool="search_media",
                partial_errors=range(9),
                now=10,
            )

        capped = store.create(
            [1],
            user_id=1,
            chat_id=2,
            tool="search_media",
            total=MAX_SNAPSHOT_ITEMS + 1,
            now=11,
        )
        self.assertEqual(capped.total, MAX_SNAPSHOT_ITEMS)
        self.assertTrue(capped.truncated)


class SafeViewContractTests(unittest.TestCase):
    def test_page_defaults_and_maxima_are_frozen(self) -> None:
        self.assertEqual(normalize_page_size("search_media"), 25)
        self.assertEqual(normalize_page_size("browse_library", 100), 100)
        self.assertEqual(normalize_page_size("download_status"), 100)
        self.assertEqual(normalize_page_size("request_status", 250), 250)
        with self.assertRaises(PageSizeError):
            normalize_page_size("search_media", 101)
        with self.assertRaises(PageSizeError):
            normalize_page_size("request_status", 251)

    def test_provider_fields_are_allow_listed_and_custom_plex_link_is_validated(
        self,
    ) -> None:
        link = build_plex_link("http://plex:32400", "machine", "123")
        item = sanitize_library_item(
            {
                "ratingKey": "123",
                "type": "movie",
                "title": "Dune",
                "year": 2021,
                "Location": [{"path": "/movies/Dune"}],
                "token": "do-not-return",
                "plex_url": link,
            },
            plex_origins=("http://plex:32400",),
        )
        payload = serialize_response(SafePage(items=(item,)))
        self.assertIn(b"Dune", payload)
        self.assertIn(b"plex_url", payload)
        self.assertNotIn(b"/movies/Dune", payload)
        self.assertNotIn(b"do-not-return", payload)
        with self.assertRaises(UnsafeProviderDataError):
            serialize_response({"items": [{"title": "raw provider object"}]})

    def test_candidate_identity_is_not_in_shared_payload(self) -> None:
        candidate = MediaCandidate(MediaType.MOVIE, 12345, "Dune")
        payload = serialize_response(SafePage(items=(candidate,)))
        self.assertNotIn(b"12345", payload)
        self.assertNotIn(b"provider_id", payload)

    def test_operation_allowlist_preserves_every_typed_safe_record_field(self) -> None:
        records = (
            MediaCandidate(
                MediaType.SERIES,
                411959,
                "3 Body Problem",
                2024,
                "A science-fiction series.",
                candidate_handle="A" * 43,
            ),
            QueueItem(
                ServiceName.SONARR,
                "3 Body Problem S01E01",
                QueueState.DOWNLOADING,
                progress_percent=42.5,
                eta_seconds=600,
                error="waiting",
                media_type=MediaType.SERIES,
            ),
            SafeLibraryItem(
                MediaType.MOVIE,
                "Dune",
                year=2021,
                library_name="Movies",
                quality="2160p",
                added_at=datetime(2026, 8, 15, tzinfo=timezone.utc),
            ),
            MediaStatus(
                MediaIdentity(MediaType.MOVIE, tmdb_id=438631, provider_id="438631"),
                True,
                title="Dune",
                year=2021,
                quality="2160p",
                as_of=datetime(2026, 8, 15, tzinfo=timezone.utc),
            ),
            SafeServiceHealth(ServiceName.RADARR, True, version="6.0", message="ready"),
            SafeRequestStatus(
                "Dune",
                MediaType.MOVIE,
                "downloading",
                year=2021,
                progress_percent=50,
                eta_seconds=300,
                quality="2160p",
            ),
        )
        error = PartialError(ServiceName.SONARR, "queue_unavailable", "temporary", True)
        for record in records:
            with self.subTest(record=type(record).__name__):
                expected = serialize_record(record)
                result = safe_operation_result(
                    SafePage(items=(record,), partial_errors=(error,)),
                    tool="search_media",
                )
                self.assertEqual(result["items"][0], expected)
                self.assertEqual(result["partial_errors"][0], serialize_record(error))

    def test_paths_and_uri_schemes_are_rejected_from_untrusted_text(self) -> None:
        for title in (r"\\nas\share\movie", "ssh://nas/movie", "custom:payload"):
            with self.subTest(title=title):
                with self.assertRaises(UnsafeProviderDataError):
                    sanitize_queue_item(
                        {"service": "radarr", "title": title, "state": "queued"}
                    )

    def test_response_size_is_enforced(self) -> None:
        candidates = tuple(
            MediaCandidate(MediaType.MOVIE, index + 1, "x" * 500)
            for index in range(MAX_SNAPSHOT_ITEMS)
        )
        with self.assertRaises(ResponseTooLargeError):
            serialize_response(SafePage(items=candidates))
        page = bounded_page(candidates[:2], tool="search_media")
        self.assertLessEqual(len(serialize_response(page)), MAX_RESPONSE_BYTES)

    def test_paginator_caps_snapshot_and_returns_signed_continuation(self) -> None:
        signer = CursorSigner(b"view-key")
        paginator = SafeViewPaginator(signer)
        items = (
            MediaCandidate(MediaType.MOVIE, index + 1, f"Movie {index}")
            for index in range(101)
        )
        first = paginator.page(
            items,
            tool="search_media",
            user_id=5,
            chat_id=6,
            limit=25,
            query={"query": "Movie"},
            normalize=lambda value: value,
            now=10,
        )
        self.assertEqual(len(first.items), 25)
        self.assertIsNotNone(first.next_cursor)
        second = paginator.page(
            cursor=first.next_cursor,
            tool="search_media",
            user_id=5,
            chat_id=6,
            query={"query": "Movie"},
            now=11,
        )
        self.assertEqual(second.items[0].title, "Movie 25")

    def test_paginator_rejects_raw_records_before_snapshot_creation(self) -> None:
        signer = CursorSigner(b"normalize-key")
        snapshots = SnapshotStore(signer)
        paginator = SafeViewPaginator(signer, snapshots=snapshots)
        with self.assertRaises(SafeViewError):
            paginator.page(
                (MediaCandidate(MediaType.MOVIE, 1, "Dune"),),
                tool="search_media",
                user_id=1,
                chat_id=2,
                now=10,
            )
        self.assertEqual(len(snapshots), 0)
        with self.assertRaises(UnsafeProviderDataError):
            paginator.page(
                [{"title": "Dune", "provider_id": 7, "secret": "taint"}],
                tool="search_media",
                user_id=1,
                chat_id=2,
                normalize=lambda value: value,
                now=10,
            )
        self.assertEqual(len(snapshots), 0)

    def test_continuation_uses_frozen_snapshot_metadata(self) -> None:
        signer = CursorSigner(b"metadata-key")
        paginator = SafeViewPaginator(signer)
        first = paginator.page(
            (
                MediaCandidate(MediaType.MOVIE, index + 1, f"Movie {index}")
                for index in range(30)
            ),
            tool="search_media",
            user_id=1,
            chat_id=2,
            query={"query": "Movie"},
            as_of=datetime(2026, 1, 1, tzinfo=timezone.utc),
            total=999,
            partial_errors=(
                {"service": "tmdb", "code": "timeout", "message": "retry"},
            ),
            normalize=lambda value: value,
            now=10,
        )
        assert first.next_cursor is not None
        second = paginator.page(
            cursor=first.next_cursor,
            tool="search_media",
            user_id=1,
            chat_id=2,
            query={"query": "Movie"},
            as_of=datetime(2030, 1, 1, tzinfo=timezone.utc),
            total=1,
            partial_errors=(
                {"service": "plex", "code": "changed", "message": "ignore"},
            ),
            now=11,
        )
        self.assertEqual(second.as_of, first.as_of)
        self.assertEqual(second.total, first.total)
        self.assertEqual(second.partial_errors, first.partial_errors)

    def test_direct_pages_cannot_accept_caller_continuations(self) -> None:
        signer = CursorSigner(b"direct-key")
        token = signer.issue(
            user_id=1,
            chat_id=2,
            tool="search_media",
            query=None,
            snapshot_id="ABCDEFGHIJKLMNOPQRSTUVWX",
            now=10,
        )
        candidate = MediaCandidate(MediaType.MOVIE, 1, "Dune")
        with self.assertRaises(SafeViewError):
            bounded_page((candidate,), next_cursor=token)
        with self.assertRaises(SafeViewError):
            SafePage(items=(candidate,), next_cursor=token)

    def test_total_cap_sets_truncated(self) -> None:
        candidate = MediaCandidate(MediaType.MOVIE, 1, "Dune")
        page = SafePage(items=(candidate,), total=MAX_SNAPSHOT_ITEMS + 100)
        self.assertEqual(page.total, MAX_SNAPSHOT_ITEMS)
        self.assertTrue(page.truncated)

    def test_continuation_requires_query_binding_and_signed_shape(self) -> None:
        signer = CursorSigner(b"binding-key")
        paginator = SafeViewPaginator(signer)
        first = paginator.page(
            (
                MediaCandidate(MediaType.MOVIE, index + 1, f"Movie {index}")
                for index in range(30)
            ),
            tool="search_media",
            user_id=7,
            chat_id=8,
            query={"query": "Movie"},
            normalize=lambda value: value,
            now=10,
        )
        assert first.next_cursor is not None
        with self.assertRaises(CursorBindingError):
            paginator.page(
                cursor=first.next_cursor,
                tool="search_media",
                user_id=7,
                chat_id=8,
                now=11,
            )
        with self.assertRaises(SafeViewError):
            SafePage(items=(), next_cursor="provider-page-2")


if __name__ == "__main__":
    unittest.main()
